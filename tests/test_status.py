"""Tests for status.py's formatting helpers. No database."""
from datetime import datetime, timedelta, timezone

import status

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


def row(**over):
    base = dict(run_id=3, job="fill", started_at=T0, finished_at=T0 + timedelta(minutes=5, seconds=7),
                status="ok", requests_budget=10000, requests_used=9876,
                stops={"backfill": "budget"}, error=None)
    return {**base, **over}


def test_fmt_duration():
    assert status.fmt_duration(T0, T0 + timedelta(seconds=45)) == "45s"
    assert status.fmt_duration(T0, T0 + timedelta(minutes=5, seconds=7)) == "5m07s"
    assert status.fmt_duration(T0, T0 + timedelta(hours=1, minutes=2)) == "1h02m"
    assert status.fmt_duration(T0, None) == "-"


def test_fmt_epoch_and_unset_cursor():
    assert status.fmt_epoch(None) == "not set"
    assert status.fmt_epoch(0).startswith("1970-01-01")


def test_describe_run_shows_budget_and_why_it_stopped():
    line = status.describe_run(row(), active_now=False)
    assert "#3" in line and "fill" in line and "9,876/10,000 requests" in line
    assert "backfill: budget" in line


def test_a_running_row_with_no_live_run_is_reported_as_interrupted():
    line = status.describe_run(row(status="running", finished_at=None, requests_used=None), active_now=False)
    assert "interrupted" in line
    assert "running" in status.describe_run(row(status="running", finished_at=None), active_now=True)


def test_describe_run_includes_the_error():
    line = status.describe_run(row(status="failed", error="ExtractError: boom"), active_now=False)
    assert "failed" in line and "error: ExtractError: boom" in line
