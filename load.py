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
in the database is updated instead of duplicated. questions and answers also carry a guard
(only overwrite when the incoming last_activity_date is the same or newer), so replaying an
old raw run can never roll newer data back. Each load() call is one transaction, and the
cursors in etl_state (see set_state) are written inside it: if any step fails, neither the
rows nor the cursor are kept.

Design decisions are written up in the README ("Schema and ERD" and "Load layer").

Run standalone on a saved extract run (no API calls):
    python load.py data/raw/<run_stamp>
"""
import sys
from collections import namedtuple

import pandas as pd
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, ForeignKey, Index, Integer, MetaData, PrimaryKeyConstraint,
    String, Table, func, select,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, insert
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


# Pipeline bookkeeping, not part of the data model. One row per cursor:
#   "<site>:questions"  max last_activity_date loaded       (update.py walks forward from it)
#   "<site>:backfill"   min creation_date loaded            (fill.py walks backward from it)
# Both store a Unix epoch in last_activity_epoch (the column keeps its original name).
etl_state = Table(
    "etl_state", metadata,
    Column("source", String(100), primary_key=True),
    Column("last_activity_epoch", BigInteger, nullable=False),
    Column("last_run_at", TIMESTAMPTZ, nullable=False),
)


# One row per fill/update run, written when it starts and finished when it ends, so anyone
# can see what ran, how much it spent and why it stopped (python status.py shows it).
etl_runs = Table(
    "etl_runs", metadata,
    Column("run_id", BigInteger, primary_key=True, autoincrement=True),
    Column("job", String(20), nullable=False),               # 'fill' or 'update'
    Column("started_at", TIMESTAMPTZ, nullable=False),
    Column("finished_at", TIMESTAMPTZ, nullable=True),       # NULL while running (or if it was killed)
    Column("status", String(20), nullable=False),            # 'running', 'ok' or 'failed'
    Column("requests_budget", Integer, nullable=False),
    Column("requests_used", Integer, nullable=True),
    Column("stops", JSONB, nullable=True),                   # stream -> why it stopped
    Column("stats", JSONB, nullable=True),                   # rows inserted/updated, etc.
    Column("error", String(1000), nullable=True),
)


def create_schema(conn):
    """Create every table, key and index that doesn't exist yet. Safe to run every time."""
    metadata.create_all(conn, checkfirst=True)


# --- run history (etl_runs) ------------------------------------------------------------

def start_run(engine, job, budget):
    """Record that a run started. Returns its run_id."""
    with engine.begin() as conn:
        return conn.execute(insert(etl_runs).values(
            job=job, started_at=func.now(), status="running", requests_budget=budget,
        ).returning(etl_runs.c.run_id)).scalar()


def finish_run(engine, run_id, status, requests_used, stops, stats, error=None):
    """Close out a run's row. `stats` and `stops` must be JSON-friendly (numbers and text)."""
    with engine.begin() as conn:
        conn.execute(etl_runs.update().where(etl_runs.c.run_id == run_id).values(
            finished_at=func.now(), status=status, requests_used=requests_used,
            stops=stops, stats=stats, error=(error or None) and error[:1000]))


# --- cursors (etl_state) ---------------------------------------------------------------

def forward_source(site):
    return f"{site}:questions"


def backfill_source(site):
    return f"{site}:backfill"


def get_state(conn, source):
    """The stored cursor epoch for `source`, or None if it has never been set."""
    return conn.execute(select(etl_state.c.last_activity_epoch)
                        .where(etl_state.c.source == source)).scalar()


def set_state(conn, source, epoch, direction):
    """Move a cursor, but only in its own direction.

    "forward" keeps the larger epoch (GREATEST), "backward" the smaller (LEAST), so a replay or a
    race can never push a cursor the wrong way. A first write just inserts the value.
    """
    if direction not in ("forward", "backward"):
        raise ValueError(f"direction must be 'forward' or 'backward', got {direction!r}")
    stmt = insert(etl_state).values(source=source, last_activity_epoch=epoch, last_run_at=func.now())
    pick = func.greatest if direction == "forward" else func.least
    conn.execute(stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={"last_activity_epoch": pick(etl_state.c.last_activity_epoch,
                                          stmt.excluded.last_activity_epoch),
              "last_run_at": func.now()}))


def seed_state(conn, source, epoch):
    """Create a cursor if it doesn't exist yet; never changes an existing one."""
    conn.execute(insert(etl_state).values(source=source, last_activity_epoch=epoch,
                                          last_run_at=func.now()).on_conflict_do_nothing())


def existing_counts(conn):
    """{(scope, month): is_partial_month} for every monthly count already stored."""
    rows = conn.execute(select(tag_monthly_counts.c.scope, tag_monthly_counts.c.month,
                               tag_monthly_counts.c.is_partial_month))
    return {(scope, month): partial for scope, month, partial in rows}


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


UpsertResult = namedtuple("UpsertResult", "inserted updated skipped keys")


def upsert(conn, table, rows, newer_col=None):
    """INSERT ... ON CONFLICT (primary key) DO UPDATE every non-key column.

    With newer_col (e.g. "last_activity_date"), an existing row is only overwritten when the
    incoming value is the same or newer; older data is skipped, so replaying an old raw run
    can't roll newer rows back. Same-or-newer still refreshes the row, because view counts and
    scores change without new activity.

    Returns UpsertResult(inserted, updated, skipped, keys). `keys` are the primary-key tuples
    of the rows actually written. `RETURNING (xmax = 0)` is true for a brand-new row and false
    for a row that already existed and was updated.
    """
    if not rows:
        return UpsertResult(0, 0, 0, [])
    key = [c.name for c in table.primary_key.columns]
    inserted = updated = skipped = 0
    written = []
    for chunk in chunks(rows):
        stmt = insert(table).values(chunk)
        non_key = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in key}
        if non_key:
            guard = table.c[newer_col] <= stmt.excluded[newer_col] if newer_col else None
            stmt = stmt.on_conflict_do_update(index_elements=key, set_=non_key, where=guard)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=key)
        returned = conn.execute(stmt.returning(
            *[table.c[k] for k in key], literal_column("(xmax = 0)").label("inserted"))).all()
        new = sum(1 for r in returned if r[-1])
        inserted += new
        updated += len(returned) - new
        skipped += len(chunk) - len(returned)
        written += [tuple(r[:-1]) for r in returned]
    return UpsertResult(inserted, updated, skipped, written)


def dedupe(df, key):
    """Keep the last row per key. Postgres rejects one statement that hits the same key twice."""
    return df.drop_duplicates(subset=key, keep="last")


# --- step 4: question_tags, replaced per question --------------------------------------

def replace_question_tags(conn, frame):
    """Delete the stored tags of every question in `frame`, then insert its current ones.

    A question's tags can be edited on Stack Overflow. A plain ON CONFLICT DO NOTHING would
    add new tags but never remove old ones, so the batch's tag set replaces the stored one.
    The caller passes only the questions the upsert actually wrote, so a stale replay can't
    roll tags back. Returns (rows_deleted, rows_inserted).
    """
    ids = [to_python(i) for i in frame["question_id"].dropna().unique()]
    deleted = 0
    for chunk in chunks(ids):
        deleted += conn.execute(
            question_tags.delete().where(question_tags.c.question_id.in_(chunk))).rowcount
    rows = frame_to_rows(dedupe(frame, ["question_id", "tag_name"]), ["question_id", "tag_name"])
    return deleted, upsert(conn, question_tags, rows).inserted


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


def load(engine, result, state=None):
    """Create the schema if needed and upsert every table of a TransformResult.

    Runs in one transaction, in foreign-key order (parents before children). `state` is a
    list of (source, epoch, direction) cursors written inside the same transaction, so rows
    and cursor commit together or not at all. Returns stats for the run summary.
    """
    stats = {}
    with engine.begin() as conn:
        create_schema(conn)

        q = dedupe(result.questions, ["question_id"])
        q_res = upsert(conn, questions, frame_to_rows(q, QUESTION_COLUMNS), newer_col="last_activity_date")
        stats["questions"] = {"inserted": q_res.inserted, "updated": q_res.updated,
                              "skipped_stale": q_res.skipped}

        # Tags are only touched for questions the upsert wrote (not stale ones).
        written = {k[0] for k in q_res.keys}
        pairs = result.question_tags[result.question_tags["question_id"].isin(written)]
        tag_names = pd.DataFrame({"tag_name": sorted(pairs["tag_name"].dropna().unique())})
        t_res = upsert(conn, tags, frame_to_rows(tag_names, ["tag_name"]))
        stats["tags"] = {"inserted": t_res.inserted, "updated": t_res.updated}

        deleted, ins = replace_question_tags(conn, pairs)
        stats["question_tags"] = {"replaced": deleted, "inserted": ins}

        a = dedupe(result.answers, ["answer_id"])
        a, skipped = drop_orphan_answers(conn, a, q["question_id"].dropna().tolist())
        a_res = upsert(conn, answers, frame_to_rows(a, ANSWER_COLUMNS), newer_col="last_activity_date")
        stats["answers"] = {"inserted": a_res.inserted, "updated": a_res.updated,
                            "skipped_stale": a_res.skipped, "orphans_skipped": skipped}

        c = dedupe(result.monthly_counts, ["scope", "month"])
        c_res = upsert(conn, tag_monthly_counts, frame_to_rows(c, COUNT_COLUMNS))
        stats["monthly_counts"] = {"inserted": c_res.inserted, "updated": c_res.updated}

        for source, epoch, direction in state or []:
            set_state(conn, source, epoch, direction)
    return stats


def summary_lines(stats):
    """Lines for the ETL run summary."""
    def iu(s):
        stale = f", {s['skipped_stale']:,} skipped (older than stored)" if s.get("skipped_stale") else ""
        return f"{s['inserted']:,} inserted, {s['updated']:,} updated{stale}"

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
