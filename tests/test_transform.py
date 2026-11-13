"""Tests for transform.py. Each test builds a tiny input by hand with the problem the
function fixes, then checks the output. No API or database needed."""
from datetime import date
from types import SimpleNamespace

import pandas as pd

import transform as t


def q(**fields):
    """A raw question dict like the API returns, with sensible defaults."""
    base = {"question_id": 1, "title": "How do I sort a list?", "tags": ["python"],
            "score": 1, "view_count": 10, "answer_count": 0, "comment_count": 0,
            "is_answered": False, "creation_date": 1700000000,
            "last_activity_date": 1700000000, "link": "https://stackoverflow.com/q/1"}
    return {**base, **fields}


# --- building frames ------------------------------------------------------------------

def test_to_frame_adds_optional_columns_no_item_has():
    df = t.to_frame([q()], t.QUESTION_COLUMNS)
    assert list(df.columns) == t.QUESTION_COLUMNS
    assert df["closed_date"].isna().all()  # no closed question in the batch, column still exists


def test_to_frame_drops_unexpected_fields():
    df = t.to_frame([{"answer_id": 1, "question_id": 2, "body": "<p>hi</p>"}], t.ANSWER_COLUMNS)
    assert "body" not in df.columns


# --- cleaning functions ---------------------------------------------------------------

def test_drop_missing_keys_removes_rows_without_id():
    df = pd.DataFrame({"question_id": [1, None, 3]})
    out, dropped = t.drop_missing_keys(df, "question_id")
    assert dropped == 1
    assert out["question_id"].tolist() == [1, 3]


def test_convert_epoch_columns_makes_utc_datetimes_and_keeps_missing():
    df = pd.DataFrame({"creation_date": [0, 1700000000], "closed_date": [None, 1700003600]})
    out = t.convert_epoch_columns(df, ["creation_date", "closed_date"])
    assert out["creation_date"][0] == pd.Timestamp("1970-01-01", tz="UTC")
    assert out["creation_date"][1] == pd.Timestamp("2023-11-14 22:13:20", tz="UTC")
    assert pd.isna(out["closed_date"][0])
    assert str(out["closed_date"].dt.tz) == "UTC"


def test_decode_html_entities_in_title():
    df = pd.DataFrame({"title": ["Why doesn&#39;t x &amp;&amp; y work?", None]})
    out = t.decode_html_entities(df, "title")
    assert out["title"][0] == "Why doesn't x && y work?"
    assert pd.isna(out["title"][1])


def test_strip_whitespace_trims_and_collapses():
    df = pd.DataFrame({"title": ["  too   many  spaces "]})
    assert t.strip_whitespace(df, ["title"])["title"][0] == "too many spaces"


def test_normalize_tags_lowercases_trims_and_dedupes():
    df = pd.DataFrame({"tags": [[" Python", "python", "PANDAS", ""], None]})
    out = t.normalize_tags(df)
    assert out["tags"][0] == ["python", "pandas"]
    assert out["tags"][1] == []


def test_remove_duplicates_keeps_newest_activity():
    df = pd.DataFrame({"question_id": [1, 1, 2], "last_activity_date": [100, 200, 50],
                       "score": [0, 5, 1]})
    out, removed = t.remove_duplicates(df, "question_id")
    assert removed == 1
    assert out.loc[out["question_id"] == 1, "score"].item() == 5


def test_fill_missing_fills_only_chosen_columns_and_reports_counts():
    df = pd.DataFrame({"score": [None, 2], "accepted_answer_id": [None, 9]})
    out, report = t.fill_missing(df, {"score": 0})
    assert out["score"].tolist() == [0, 2]
    assert report == {"score": 1}
    assert pd.isna(out["accepted_answer_id"][0])  # NULL means "no accepted answer", left alone


def test_cast_types_keeps_nulls_in_integer_columns():
    df = pd.DataFrame({"accepted_answer_id": [None, "42"], "is_answered": [True, False]})
    out = t.cast_types(df, {"accepted_answer_id": "Int64", "is_answered": "boolean"})
    assert str(out["accepted_answer_id"].dtype) == "Int64"
    assert pd.isna(out["accepted_answer_id"][0]) and out["accepted_answer_id"][1] == 42
    assert str(out["is_answered"].dtype) == "boolean"


def test_standardize_closed_reason():
    df = pd.DataFrame({"closed_reason": [" duplicate ", "DUPLICATE", "needs  more focus", "", None]})
    out = t.standardize_closed_reason(df)["closed_reason"].tolist()
    assert out[:3] == ["Duplicate", "Duplicate", "Needs more focus"]
    assert pd.isna(out[3]) and pd.isna(out[4])


def test_add_closed_flags():
    df = pd.DataFrame({"closed_date": [pd.NaT, pd.Timestamp("2026-01-01", tz="UTC"),
                                       pd.Timestamp("2026-01-02", tz="UTC")],
                       "closed_reason": [pd.NA, "Duplicate", "Opinion-based"]})
    out = t.add_closed_flags(df)
    assert out["is_closed"].tolist() == [False, True, True]
    assert out["is_duplicate_closure"].tolist() == [False, True, False]


def test_explode_tags_makes_one_row_per_pair():
    df = pd.DataFrame({"question_id": [1, 2, 3], "tags": [["python", "pandas"], ["python"], []]})
    out = t.explode_tags(df)
    assert sorted(map(tuple, out[["question_id", "tag_name"]].values.tolist())) == [
        (1, "pandas"), (1, "python"), (2, "python")]


def test_build_tags_table_is_distinct_and_sorted():
    pairs = pd.DataFrame({"question_id": [1, 1, 2], "tag_name": ["python", "pandas", "python"]})
    assert t.build_tags_table(pairs)["tag_name"].tolist() == ["pandas", "python"]


def test_parse_month_turns_string_into_first_of_month():
    df = pd.DataFrame({"month": ["2026-03", "bad"]})
    out = t.parse_month(df)
    assert out["month"][0] == date(2026, 3, 1)
    assert pd.isna(out["month"][1])


def test_normalize_scope():
    df = pd.DataFrame({"scope": [" Python", "site"]})
    assert t.normalize_scope(df)["scope"].tolist() == ["python", "site"]


def test_drop_missing_counts():
    df = pd.DataFrame({"question_count": [10, None]})
    out, dropped = t.drop_missing_counts(df)
    assert dropped == 1 and out["question_count"].tolist() == [10]


# --- whole pipeline -------------------------------------------------------------------

def test_transform_end_to_end():
    raw = SimpleNamespace(
        questions=[
            q(question_id=1, title="A &amp; B", tags=["Python", "pandas"], last_activity_date=100),
            q(question_id=1, title="A &amp; B (edited)", tags=["python"], last_activity_date=200),
            q(question_id=2, tags=["python"], closed_date=1700000500, closed_reason="duplicate",
              accepted_answer_id=11, answer_count=1),
            q(question_id=None),
        ],
        answers=[
            {"answer_id": 11, "question_id": 2, "score": 3, "is_accepted": True,
             "creation_date": 1700000100, "last_activity_date": 1700000100},
            {"answer_id": 11, "question_id": 2, "score": 4, "is_accepted": True,
             "creation_date": 1700000100, "last_activity_date": 1700000200},
        ],
        monthly_counts=[
            {"scope": "site", "month": "2026-10", "question_count": 5000, "is_partial_month": False},
            {"scope": "Python", "month": "2026-10", "question_count": None, "is_partial_month": False},
        ],
    )
    out = t.transform(raw)

    assert out.questions["question_id"].tolist() == [1, 2]
    assert out.questions.loc[0, "title"] == "A & B (edited)"
    assert "tags" not in out.questions.columns
    assert out.questions.loc[1, "is_duplicate_closure"]
    assert out.question_tags["tag_name"].tolist() == ["python", "python"]
    assert out.tags["tag_name"].tolist() == ["python"]
    assert out.answers["score"].tolist() == [4]
    assert out.monthly_counts["scope"].tolist() == ["site"]

    s = out.stats
    assert s["questions"]["received"] == 4
    assert s["questions"]["duplicates_removed"] == 1
    assert s["questions"]["missing_id_dropped"] == 1
    assert s["answers"]["duplicates_removed"] == 1
    assert s["monthly_counts"]["missing_count_dropped"] == 1
    assert all(isinstance(line, str) for line in t.summary_lines(s))


def test_transform_handles_empty_incremental_run():
    out = t.transform(SimpleNamespace(questions=[], answers=[], monthly_counts=[]))
    assert out.questions.empty and out.answers.empty and out.tags.empty
    assert out.stats["questions"]["after_cleaning"] == 0
    t.summary_lines(out.stats)
