"""Tests for load.py that need no database: they check the SQL that would be sent."""
import pandas as pd
from sqlalchemy.dialects import postgresql

import load


class RecordingConn:
    """Stands in for a connection: records each statement and returns canned rows."""

    def __init__(self, rows=()):
        self.statements, self.rows = [], list(rows)

    def execute(self, stmt):
        self.statements.append(stmt)
        outer = self

        class Result:
            def all(self):
                return outer.rows

        return Result()


def sql(stmt):
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_question_upsert_only_overwrites_with_same_or_newer_activity():
    conn = RecordingConn()
    load.upsert(conn, load.questions, [{"question_id": 1}], newer_col="last_activity_date")
    text = sql(conn.statements[0])
    assert "ON CONFLICT (question_id) DO UPDATE" in text
    assert "WHERE questions.last_activity_date <= excluded.last_activity_date" in text


def test_upsert_without_newer_col_has_no_guard():
    conn = RecordingConn()
    load.upsert(conn, load.tag_monthly_counts, [{"scope": "site", "month": 1}])
    assert "WHERE" not in sql(conn.statements[0])


def test_upsert_counts_inserted_updated_and_skipped_rows():
    # Three rows sent; Postgres returned two (the stale one was skipped by the guard):
    # one new (xmax = 0) and one updated.
    conn = RecordingConn(rows=[(1, True), (2, False)])
    res = load.upsert(conn, load.questions, [{"question_id": n} for n in (1, 2, 3)],
                      newer_col="last_activity_date")
    assert (res.inserted, res.updated, res.skipped) == (1, 1, 1)
    assert res.keys == [(1,), (2,)]


def test_upsert_of_nothing_sends_nothing():
    conn = RecordingConn()
    assert load.upsert(conn, load.questions, []).keys == []
    assert conn.statements == []


def test_forward_cursor_can_only_move_forward():
    conn = RecordingConn()
    load.set_state(conn, "stackoverflow:questions", 100, "forward")
    assert "greatest(etl_state.last_activity_epoch, excluded.last_activity_epoch)" in sql(conn.statements[0])


def test_backfill_cursor_can_only_move_backward():
    conn = RecordingConn()
    load.set_state(conn, "stackoverflow:backfill", 100, "backward")
    assert "least(etl_state.last_activity_epoch, excluded.last_activity_epoch)" in sql(conn.statements[0])


def test_seeding_a_cursor_never_overwrites_an_existing_one():
    conn = RecordingConn()
    load.seed_state(conn, "stackoverflow:backfill", 100)
    assert "ON CONFLICT DO NOTHING" in sql(conn.statements[0])


def test_set_state_rejects_an_unknown_direction():
    import pytest
    with pytest.raises(ValueError):
        load.set_state(RecordingConn(), "x", 1, "sideways")


def test_cursor_sources_are_per_site():
    assert load.forward_source("stackoverflow") == "stackoverflow:questions"
    assert load.backfill_source("stackoverflow") == "stackoverflow:backfill"


def test_to_python_turns_pandas_missing_values_into_none():
    assert load.to_python(pd.NA) is None and load.to_python(pd.NaT) is None
    assert load.to_python(float("nan")) is None


def test_to_python_turns_numpy_and_timestamps_into_plain_values():
    import numpy as np
    assert type(load.to_python(np.int64(5))) is int
    assert load.to_python(pd.Timestamp("2026-01-01", tz="UTC")).year == 2026
