"""Load: create the PostgreSQL schema and upsert the cleaned tables from transform.py.

Input:  a TransformResult (one pandas DataFrame per table).
Output: a dict of inserted / updated / skipped counts per table, for the run summary.

    questions(question_id PK, ...)
    answers(answer_id PK, question_id FK -> questions)
    tags(tag_name PK)
    question_tags(question_id FK -> questions, tag_name FK -> tags)   PK (question_id, tag_name)
    tag_monthly_counts(scope, month, ...)                             PK (scope, month)

Every table and key is defined below with SQLAlchemy Core, never inferred by pandas.
Every write is an INSERT ... ON CONFLICT (primary key) DO UPDATE, so a row that is already
in the database is updated instead of duplicated. The whole run is one transaction: if any
step fails, nothing from that run is kept, and the watermark is not advanced.

Design decisions are written up in the README ("Schema and ERD" and "Load layer").

Run standalone on a saved extract run (no API calls):
    python load.py data/raw/<run_stamp>
"""
import sys

import pandas as pd
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, ForeignKey, Index, Integer, MetaData, PrimaryKeyConstraint,
    String, Table, select,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, insert
from sqlalchemy.sql import literal_column

# Rows per INSERT statement. Postgres allows 65,535 bind parameters per statement;
# 1,000 rows x 16 columns (questions, the widest table) stays far below that.
CHUNK_SIZE = 1000

TIMESTAMPTZ = TIMESTAMP(timezone=True)

# --- step 1: schema, defined in our own code ------------------------------------------

metadata = MetaData()

questions = Table(
    "questions", metadata,
    Column("question_id", BigInteger, primary_key=True, autoincrement=False),
    Column("title", String(300), nullable=False),
    Column("score", Integer, nullable=False),
    Column("view_count", Integer, nullable=False),
    Column("answer_count", Integer, nullable=False),
    Column("comment_count", Integer, nullable=False),
    Column("is_answered", Boolean, nullable=False),
    # No FK to answers: the accepted answer may never be pulled (see README).
    Column("accepted_answer_id", BigInteger, nullable=True),
    Column("creation_date", TIMESTAMPTZ, nullable=False),
    Column("last_activity_date", TIMESTAMPTZ, nullable=False),
    Column("last_edit_date", TIMESTAMPTZ, nullable=True),    # NULL = never edited
    Column("closed_date", TIMESTAMPTZ, nullable=True),       # NULL = still open
    Column("closed_reason", String(50), nullable=True),
    Column("is_closed", Boolean, nullable=False),
    Column("is_duplicate_closure", Boolean, nullable=False),
    Column("link", String(500), nullable=False),
    Index("ix_questions_creation_date", "creation_date"),
)

answers = Table(
    "answers", metadata,
    Column("answer_id", BigInteger, primary_key=True, autoincrement=False),
    Column("question_id", BigInteger,
           ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False),
    Column("score", Integer, nullable=False),
    Column("is_accepted", Boolean, nullable=False),
    Column("creation_date", TIMESTAMPTZ, nullable=False),
    Column("last_activity_date", TIMESTAMPTZ, nullable=False),
    Column("last_edit_date", TIMESTAMPTZ, nullable=True),
    Index("ix_answers_question_id", "question_id"),
)

tags = Table(
    "tags", metadata,
    # Stack Overflow caps tag names at 35 characters; 64 leaves room.
    Column("tag_name", String(64), primary_key=True),
)

question_tags = Table(
    "question_tags", metadata,
    Column("question_id", BigInteger,
           ForeignKey("questions.question_id", ondelete="CASCADE"), nullable=False),
    Column("tag_name", String(64), ForeignKey("tags.tag_name"), nullable=False),
    PrimaryKeyConstraint("question_id", "tag_name"),
    Index("ix_question_tags_tag_name", "tag_name"),
)

tag_monthly_counts = Table(
    "tag_monthly_counts", metadata,
    # 'site' or a tag name. No FK to tags: 'site' is not a tag, and a tracked tag may not
    # have appeared in any pulled question yet (see README).
    Column("scope", String(64), nullable=False),
    Column("month", Date, nullable=False),                   # first day of the month
    Column("question_count", Integer, nullable=False),
    Column("is_partial_month", Boolean, nullable=False),
    PrimaryKeyConstraint("scope", "month"),
)


def create_schema(conn):
    """Create every table, key and index that doesn't exist yet. Safe to run every time."""
    metadata.create_all(conn, checkfirst=True)


# --- step 2: pandas -> plain Python values ---------------------------------------------

def to_python(value):
    """One cell -> a value psycopg2 can send. NA / NaT / NaN -> None, numpy scalars -> Python."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple, dict)) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):          # numpy int64 / bool_ / float64
        return value.item()
    return value


def frame_to_rows(df, columns):
    """DataFrame -> list of dicts restricted to `columns`, with every cell made DB-safe."""
    return [{col: to_python(v) for col, v in zip(columns, rec)}
            for rec in df[columns].itertuples(index=False, name=None)]


# --- step 3: upserts -----------------------------------------------------------------

def chunks(rows, size=CHUNK_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def upsert(conn, table, rows):
    """INSERT ... ON CONFLICT (primary key) DO UPDATE every non-key column.

    Returns (inserted, updated). `RETURNING (xmax = 0)` is true for a brand-new row and
    false for a row that already existed and was updated.
    """
    if not rows:
        return 0, 0
    key = [c.name for c in table.primary_key.columns]
    inserted = updated = 0
    for chunk in chunks(rows):
        stmt = insert(table).values(chunk)
        non_key = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in key}
        if non_key:
            stmt = stmt.on_conflict_do_update(index_elements=key, set_=non_key)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=key)
        flags = conn.execute(stmt.returning(literal_column("(xmax = 0)").label("inserted"))).scalars().all()
        new = sum(1 for f in flags if f)
        inserted += new
        updated += len(flags) - new
    return inserted, updated


def dedupe(df, key):
    """Keep the last row per key. Postgres rejects one statement that hits the same key twice."""
    return df.drop_duplicates(subset=key, keep="last")


# --- step 4: question_tags, replaced per question --------------------------------------

def replace_question_tags(conn, frame):
    """Delete the stored tags of every question in this batch, then insert its current ones.

    A question's tags can be edited on Stack Overflow. A plain ON CONFLICT DO NOTHING would
    add new tags but never remove old ones, so the batch's tag set replaces the stored one.
    Returns (rows_deleted, rows_inserted).
    """
    ids = [to_python(i) for i in frame["question_id"].dropna().unique()]
    deleted = 0
    for chunk in chunks(ids):
        deleted += conn.execute(
            question_tags.delete().where(question_tags.c.question_id.in_(chunk))).rowcount
    rows = frame_to_rows(dedupe(frame, ["question_id", "tag_name"]), ["question_id", "tag_name"])
    inserted, _ = upsert(conn, question_tags, rows)
    return deleted, inserted


# --- answers whose question is missing ------------------------------------------------

def drop_orphan_answers(conn, frame, batch_question_ids):
    """Drop answers whose question is neither in this batch nor already in the database.

    extract.py only asks for answers of questions in the same run, so this should find
    nothing. It is a guard so one odd row is counted and skipped instead of failing the
    foreign key and rolling back the whole run. Returns (frame, rows_skipped).
    """
    batch = set(batch_question_ids)
    unknown = [to_python(i) for i in frame["question_id"].dropna().unique() if i not in batch]
    known = set()
    for chunk in chunks(unknown):
        known.update(conn.execute(
            select(questions.c.question_id).where(questions.c.question_id.in_(chunk))).scalars())
    keep = frame["question_id"].isin(batch | known)
    return frame[keep], int((~keep).sum())


# --- step 5 + 7: the load ------------------------------------------------------------

QUESTION_COLUMNS = [c.name for c in questions.columns]
ANSWER_COLUMNS = [c.name for c in answers.columns]
COUNT_COLUMNS = [c.name for c in tag_monthly_counts.columns]


def load(engine, result):
    """Create the schema if needed and upsert every table of a TransformResult.

    Runs in one transaction, in foreign-key order (parents before children). Returns
    stats for the run summary; the caller saves the watermark only after this returns.
    """
    stats = {}
    with engine.begin() as conn:
        create_schema(conn)

        tag_rows = frame_to_rows(dedupe(result.tags, ["tag_name"]), ["tag_name"])
        stats["tags"] = dict(zip(("inserted", "updated"), upsert(conn, tags, tag_rows)))

        q = dedupe(result.questions, ["question_id"])
        ins, upd = upsert(conn, questions, frame_to_rows(q, QUESTION_COLUMNS))
        stats["questions"] = {"inserted": ins, "updated": upd}

        deleted, ins = replace_question_tags(conn, result.question_tags)
        stats["question_tags"] = {"replaced": deleted, "inserted": ins}

        a = dedupe(result.answers, ["answer_id"])
        a, skipped = drop_orphan_answers(conn, a, q["question_id"].dropna().tolist())
        ins, upd = upsert(conn, answers, frame_to_rows(a, ANSWER_COLUMNS))
        stats["answers"] = {"inserted": ins, "updated": upd, "orphans_skipped": skipped}

        c = dedupe(result.monthly_counts, ["scope", "month"])
        ins, upd = upsert(conn, tag_monthly_counts, frame_to_rows(c, COUNT_COLUMNS))
        stats["monthly_counts"] = {"inserted": ins, "updated": upd}
    return stats


def summary_lines(stats):
    """Lines for the ETL run summary."""
    def iu(s):
        return f"{s['inserted']:,} inserted, {s['updated']:,} updated"

    a = stats["answers"]
    orphan = f", {a['orphans_skipped']} skipped (question not found)" if a["orphans_skipped"] else ""
    return [
        f"loaded questions: {iu(stats['questions'])}",
        f"loaded answers: {iu(a)}{orphan}",
        f"loaded tags: {iu(stats['tags'])}; question-tag pairs: {stats['question_tags']['inserted']:,} "
        f"written ({stats['question_tags']['replaced']:,} old pairs replaced)",
        f"loaded monthly counts: {iu(stats['monthly_counts'])}",
    ]


def main(argv=None):
    from db import get_engine
    from extract import ExtractError, load_raw
    from transform import transform
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python load.py data/raw/<run_stamp>", file=sys.stderr)
        return 2
    try:
        stats = load(get_engine(), transform(load_raw(argv[0])))
    except (ExtractError, RuntimeError) as e:
        print(f"Load failed: {e}", file=sys.stderr)
        return 1
    for line in summary_lines(stats):
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
