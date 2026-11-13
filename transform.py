"""Transform: clean the raw Stack Exchange items from extract.py into tables for load.py.

Input:  an ExtractResult (questions, answers, monthly_counts are lists of raw API dicts).
Output: a TransformResult with one pandas DataFrame per table, plus counts for the run summary.

    questions(question_id PK, title, score, view_count, answer_count, comment_count,
              is_answered, accepted_answer_id, creation_date, last_activity_date,
              last_edit_date, closed_date, closed_reason, is_closed, is_duplicate_closure, link)
    answers(answer_id PK, question_id FK, score, is_accepted, creation_date,
            last_activity_date, last_edit_date)
    tags(tag_name PK)
    question_tags(question_id FK, tag_name FK)          PK (question_id, tag_name)
    tag_monthly_counts(scope, month, question_count, is_partial_month)   PK (scope, month)

Every cleaning step is its own small function so it can be tested on its own
(see tests/test_transform.py). The transform_* functions at the bottom only call them in order.

Run standalone on a saved extract run (no API calls, no database):
    python transform.py data/raw/<run_stamp>
"""
import html
import sys
from dataclasses import dataclass, field

import pandas as pd

# --- column lists ---------------------------------------------------------------------

QUESTION_COLUMNS = [
    "question_id", "title", "tags", "score", "view_count", "answer_count", "comment_count",
    "is_answered", "accepted_answer_id", "creation_date", "last_activity_date",
    "last_edit_date", "closed_date", "closed_reason", "link",
]
ANSWER_COLUMNS = [
    "answer_id", "question_id", "score", "is_accepted", "creation_date",
    "last_activity_date", "last_edit_date",
]
COUNT_COLUMNS = ["scope", "month", "question_count", "is_partial_month"]

QUESTION_DATES = ["creation_date", "last_activity_date", "last_edit_date", "closed_date"]
ANSWER_DATES = ["creation_date", "last_activity_date", "last_edit_date"]

# Missing-value policy, decided column by column (Assignment 3 style).
# Columns not listed here are either required keys (rows without them are dropped) or
# are left NULL on purpose because a missing value has a meaning:
#   accepted_answer_id  NULL = no accepted answer yet
#   last_edit_date      NULL = never edited
#   closed_date/_reason NULL = question is still open
QUESTION_FILLS = {
    "score": 0,            # API omits nothing here in practice; 0 is the neutral score
    "view_count": 0,
    "answer_count": 0,
    "comment_count": 0,
    "is_answered": False,
    "title": "(untitled)",
    "link": "",
}
ANSWER_FILLS = {
    "score": 0,
    "is_accepted": False,
}

QUESTION_TYPES = {
    "question_id": "Int64", "score": "Int64", "view_count": "Int64", "answer_count": "Int64",
    "comment_count": "Int64", "accepted_answer_id": "Int64", "is_answered": "boolean",
}
ANSWER_TYPES = {
    "answer_id": "Int64", "question_id": "Int64", "score": "Int64", "is_accepted": "boolean",
}


# --- building frames ------------------------------------------------------------------

def to_frame(items, columns):
    """Turn a list of API dicts into a DataFrame with exactly `columns`.

    Optional API fields are *missing* (not null) when they don't apply, so a key that no
    item has would otherwise vanish from the frame. Missing columns are added as all-NA.
    Extra fields are dropped.
    """
    df = pd.DataFrame(list(items))
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns].copy()


# --- single-purpose cleaning functions ------------------------------------------------

def drop_missing_keys(df, key):
    """Drop rows whose primary key is missing. Returns (df, rows_dropped).

    A row with no id can't be upserted or joined, so this is the one case where
    dropping is the right choice.
    """
    mask = df[key].isna()
    return df[~mask].copy(), int(mask.sum())


def convert_epoch_columns(df, columns):
    """Convert Unix-second columns to timezone-aware UTC datetimes. Missing stays NaT."""
    df = df.copy()
    for col in columns:
        df[col] = pd.to_datetime(pd.to_numeric(df[col], errors="coerce"), unit="s", utc=True)
    return df


def decode_html_entities(df, column):
    """Unescape HTML entities the API leaves in text, e.g. '&#39;' -> "'" and '&amp;' -> '&'."""
    df = df.copy()
    df[column] = df[column].map(lambda v: html.unescape(v) if isinstance(v, str) else v)
    return df


def strip_whitespace(df, columns):
    """Trim leading/trailing whitespace and collapse internal runs of spaces in text columns."""
    df = df.copy()
    for col in columns:
        df[col] = df[col].map(lambda v: " ".join(v.split()) if isinstance(v, str) else v)
    return df


def normalize_tags(df, column="tags"):
    """Lowercase and trim every tag, drop blanks, and remove repeats within one question.

    A missing tag list becomes an empty list. Order is kept (the first tag is the one the
    asker typed first).
    """
    def clean(tags):
        if not isinstance(tags, (list, tuple)):
            return []
        seen = []
        for t in tags:
            if isinstance(t, str):
                t = t.strip().lower()
                if t and t not in seen:
                    seen.append(t)
        return seen

    df = df.copy()
    df[column] = df[column].map(clean)
    return df


def remove_duplicates(df, key, newest_by="last_activity_date"):
    """Keep one row per `key`, the one with the latest `newest_by`. Returns (df, removed).

    Duplicates come from the 5-minute overlap between incremental runs and from pages
    shifting while we paginate by activity.
    """
    before = len(df)
    df = (df.sort_values(newest_by, na_position="first", kind="stable")
            .drop_duplicates(subset=key, keep="last")
            .sort_values(key, kind="stable")
            .reset_index(drop=True))
    return df, before - len(df)


def fill_missing(df, fills):
    """Fill NA in each column with its chosen value. Returns (df, {column: rows_filled})."""
    df = df.copy()
    report = {}
    for col, value in fills.items():
        n = int(df[col].isna().sum())
        if n:
            df[col] = df[col].astype(object).where(df[col].notna(), value)
        report[col] = n
    return df, report


def cast_types(df, types):
    """Cast columns to nullable pandas types (Int64, boolean) so NULLs survive into Postgres."""
    df = df.copy()
    for col, dtype in types.items():
        if dtype == "Int64":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        else:
            df[col] = df[col].astype(dtype)
    return df


def standardize_closed_reason(df, column="closed_reason"):
    """Make close reasons consistent: trimmed, first letter capitalized, blanks -> NA.

    e.g. ' duplicate ' and 'DUPLICATE' both become 'Duplicate'.
    """
    df = df.copy()

    def clean(v):
        if not isinstance(v, str) or not v.strip():
            return pd.NA
        v = " ".join(v.split()).lower()
        return v[0].upper() + v[1:]

    df[column] = df[column].map(clean)
    return df


def add_closed_flags(df):
    """Add is_closed and is_duplicate_closure.

    Team decision pending on duplicate-closed questions, so we keep the rows and flag
    them; the dashboard can filter with is_duplicate_closure = false for Q4 and Q5.
    """
    df = df.copy()
    df["is_closed"] = df["closed_date"].notna()
    df["is_duplicate_closure"] = df["closed_reason"].eq("Duplicate").fillna(False).astype(bool)
    return df


def explode_tags(df, column="tags"):
    """Return the question_tags bridge table: one (question_id, tag_name) row per pair."""
    pairs = (df[["question_id", column]]
             .explode(column)
             .dropna(subset=[column])
             .rename(columns={column: "tag_name"})
             .drop_duplicates()
             .reset_index(drop=True))
    pairs["question_id"] = pairs["question_id"].astype("Int64")
    return pairs


def build_tags_table(question_tags):
    """Return the tags table: every distinct tag_name, sorted."""
    names = sorted(question_tags["tag_name"].dropna().unique())
    return pd.DataFrame({"tag_name": names})


def parse_month(df, column="month"):
    """Turn 'YYYY-MM' strings into a date for the first of that month. Bad values -> NaT."""
    df = df.copy()
    df[column] = pd.to_datetime(df[column], format="%Y-%m", errors="coerce").dt.date
    return df


def normalize_scope(df, column="scope"):
    """Lowercase and trim the scope ('site' or a tag) so it matches tags.tag_name."""
    df = df.copy()
    df[column] = df[column].map(lambda v: v.strip().lower() if isinstance(v, str) else v)
    return df


def drop_missing_counts(df, column="question_count"):
    """Drop monthly rows with no count. Returns (df, rows_dropped).

    A count is missing only when its request failed or quota ran out. Guessing a number
    (0 or an average) would draw a fake dip or plateau on the Q1/Q2 trend charts, so the
    row is dropped and the next run fills it in.
    """
    mask = df[column].isna()
    return df[~mask].copy(), int(mask.sum())


# --- per-stream pipelines -------------------------------------------------------------

@dataclass
class TransformResult:
    questions: pd.DataFrame
    answers: pd.DataFrame
    tags: pd.DataFrame
    question_tags: pd.DataFrame
    monthly_counts: pd.DataFrame
    stats: dict = field(default_factory=dict)


def transform_questions(items):
    stats = {"received": len(items)}
    df = to_frame(items, QUESTION_COLUMNS)
    df, stats["missing_id_dropped"] = drop_missing_keys(df, "question_id")
    df = convert_epoch_columns(df, QUESTION_DATES)
    df = decode_html_entities(df, "title")
    df = strip_whitespace(df, ["title"])
    df = normalize_tags(df)
    df, stats["duplicates_removed"] = remove_duplicates(df, "question_id")
    stats["missing_dates"] = {c: int(df[c].isna().sum()) for c in QUESTION_DATES}
    df, stats["filled"] = fill_missing(df, QUESTION_FILLS)
    df = cast_types(df, QUESTION_TYPES)
    df = standardize_closed_reason(df)
    df = add_closed_flags(df)

    question_tags = explode_tags(df)
    tags = build_tags_table(question_tags)
    df = df.drop(columns=["tags"])  # tags live in question_tags, not repeated here (3NF)
    stats["after_cleaning"] = len(df)
    return df, tags, question_tags, stats


def transform_answers(items):
    stats = {"received": len(items)}
    df = to_frame(items, ANSWER_COLUMNS)
    df, stats["missing_id_dropped"] = drop_missing_keys(df, "answer_id")
    df, stats["missing_question_dropped"] = drop_missing_keys(df, "question_id")
    df = convert_epoch_columns(df, ANSWER_DATES)
    df, stats["duplicates_removed"] = remove_duplicates(df, "answer_id")
    df, stats["filled"] = fill_missing(df, ANSWER_FILLS)
    df = cast_types(df, ANSWER_TYPES)
    stats["after_cleaning"] = len(df)
    return df, stats


def transform_monthly_counts(items):
    stats = {"received": len(items)}
    df = to_frame(items, COUNT_COLUMNS)
    df = normalize_scope(df)
    df = parse_month(df)
    df, stats["bad_month_dropped"] = drop_missing_keys(df, "month")
    df, stats["missing_count_dropped"] = drop_missing_counts(df)
    before = len(df)
    df = df.drop_duplicates(subset=["scope", "month"], keep="last").reset_index(drop=True)
    stats["duplicates_removed"] = before - len(df)
    df, stats["filled"] = fill_missing(df, {"is_partial_month": False})
    df = cast_types(df, {"question_count": "Int64", "is_partial_month": "boolean"})
    stats["after_cleaning"] = len(df)
    return df, stats


def transform(result):
    """Clean every stream of an ExtractResult. Returns a TransformResult."""
    questions, tags, question_tags, q_stats = transform_questions(result.questions)
    answers, a_stats = transform_answers(result.answers)
    counts, c_stats = transform_monthly_counts(result.monthly_counts)
    return TransformResult(
        questions=questions, answers=answers, tags=tags, question_tags=question_tags,
        monthly_counts=counts,
        stats={"questions": q_stats, "answers": a_stats, "monthly_counts": c_stats,
               "question_tags": len(question_tags), "tags": len(tags)},
    )


def summary_lines(stats):
    """Lines for the ETL run summary: rows after cleaning, duplicates, missing values."""
    q, a, c = stats["questions"], stats["answers"], stats["monthly_counts"]

    def fills(report):
        parts = [f"{col} {n}" for col, n in report.items() if n]
        return ", ".join(parts) if parts else "none needed"

    return [
        f"after cleaning: {q['after_cleaning']:,} questions ({q['duplicates_removed']:,} duplicates removed, "
        f"{q['missing_id_dropped']} without an id dropped), "
        f"{a['after_cleaning']:,} answers ({a['duplicates_removed']:,} duplicates removed), "
        f"{c['after_cleaning']:,} monthly counts ({c['missing_count_dropped']} without a count dropped)",
        f"missing values filled: questions: {fills(q['filled'])}; answers: {fills(a['filled'])}",
        "missing values kept as NULL (they mean something): "
        + ", ".join(f"{col} {n}" for col, n in q["missing_dates"].items()),
        f"tags: {stats['tags']:,} distinct, {stats['question_tags']:,} question-tag pairs",
    ]


def main(argv=None):
    from extract import ExtractError, load_raw
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python transform.py data/raw/<run_stamp>", file=sys.stderr)
        return 2
    try:
        result = transform(load_raw(argv[0]))
    except ExtractError as e:
        print(f"Transform failed: {e}", file=sys.stderr)
        return 1
    for line in summary_lines(result.stats):
        print("  " + line)
    print(result.questions.head())
    return 0


if __name__ == "__main__":
    sys.exit(main())
