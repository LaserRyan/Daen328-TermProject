"""Tests for etl_common.py, fill.py and update.py. No network and no database."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import etl_common
import fill
import update
from extract import ExtractError


def test_merge_stats_adds_numbers_leaf_by_leaf():
    a = {"questions": {"inserted": 2, "updated": 1}, "tags": 3}
    b = {"questions": {"inserted": 5, "updated": 0}, "tags": 4}
    assert etl_common.merge_stats(a, b) == {"questions": {"inserted": 7, "updated": 1}, "tags": 7}


def test_merge_stats_handles_the_first_batch_and_missing_keys():
    assert etl_common.merge_stats(None, {"a": 1}) == {"a": 1}
    assert etl_common.merge_stats({"a": {"x": 1}}, {"a": {"y": 2}}) == {"a": {"x": 1, "y": 2}}


def make_ctx(**client_attrs):
    client = SimpleNamespace(stop_reason=None, pages_fetched=0, **client_attrs)
    config = SimpleNamespace(site="stackoverflow", max_pages=300, batch_pages=10,
                             backfill_floor=__import__("datetime").datetime(
                                 2025, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    return etl_common.Ctx(config, SimpleNamespace(connect=lambda: None), client, etl_common.RunTotals())


def stub_cursor(monkeypatch, value):
    @contextmanager
    def conn():
        yield object()

    monkeypatch.setattr(etl_common.loader, "get_state", lambda c, source: value)
    ctx = make_ctx()
    ctx.engine = SimpleNamespace(connect=conn)
    return ctx


def test_update_without_a_watermark_says_to_run_fill_first(monkeypatch):
    ctx = stub_cursor(monkeypatch, None)
    with pytest.raises(ExtractError, match="run fill first"):
        etl_common.run_forward(ctx)


def test_fill_past_the_floor_spends_no_api_calls(monkeypatch):
    ctx = stub_cursor(monkeypatch, 1_700_000_000)   # Nov 2023, before the 2025 floor
    monkeypatch.setattr(etl_common, "fetch_question_batch",
                        lambda *a, **k: pytest.fail("must not call the API"))
    etl_common.run_backfill(ctx)
    assert ctx.totals.stops["backfill"] == "floor"


def run_backfill_with(monkeypatch, batches, answers_complete=True):
    """Drive run_backfill with scripted batches; returns (ctx, loaded_states)."""
    ctx = stub_cursor(monkeypatch, 2_000_000_000)
    script = iter(batches)
    monkeypatch.setattr(etl_common, "fetch_question_batch", lambda *a, **k: next(script))
    monkeypatch.setattr(etl_common, "extract_answers", lambda c, ids: ([], answers_complete))
    loaded = []

    def fake_process(ctx_, questions, state):
        if not answers_complete:
            ctx_.totals.discarded += 1
            return False
        loaded.append(state)
        return True

    monkeypatch.setattr(etl_common, "_process_batch", fake_process)
    etl_common.run_backfill(ctx)
    return ctx, loaded


def q(created):
    return {"question_id": created, "creation_date": created, "last_activity_date": created}


def test_backfill_moves_its_cursor_back_each_batch_and_stops_at_the_floor(monkeypatch):
    ctx, loaded = run_backfill_with(monkeypatch, [
        ([q(1_900_000_000), q(1_800_000_000)], "page_cap"),
        ([q(1_750_000_000)], "done"),
    ])
    src = "stackoverflow:backfill"
    floor = 1_735_689_600   # 2025-01-01
    assert loaded == [[(src, 1_800_000_000, "backward")], [(src, floor, "backward")]]
    assert ctx.totals.stops["backfill"] == "floor"


def test_backfill_keeps_the_cursor_when_the_budget_ends_mid_batch(monkeypatch):
    ctx, loaded = run_backfill_with(monkeypatch, [([q(1_900_000_000)], "budget")])
    assert loaded == [[("stackoverflow:backfill", 1_900_000_000, "backward")]]
    assert ctx.totals.stops["backfill"] == "budget"


def test_a_batch_with_incomplete_answers_is_not_loaded(monkeypatch):
    ctx = stub_cursor(monkeypatch, 2_000_000_000)
    monkeypatch.setattr(etl_common, "extract_answers", lambda c, ids: ([], False))
    monkeypatch.setattr(etl_common.loader, "load", lambda *a, **k: pytest.fail("must not load"))
    ctx.client.stop_reason = "budget"
    assert etl_common._process_batch(ctx, [{"question_id": 1, "answer_count": 2}], []) is False
    assert ctx.totals.discarded == 1


def test_a_second_run_is_refused_while_the_lock_is_held():
    class Conn:
        def execution_options(self, **kw):
            return self

        def execute(self, stmt, params=None):
            return SimpleNamespace(scalar=lambda: False)

        def close(self):
            pass

    engine = SimpleNamespace(connect=lambda: Conn())
    with pytest.raises(ExtractError, match="another run is in progress"):
        with etl_common.run_lock(engine):
            pass


def test_jobs_exit_1_with_one_line_when_a_stream_fails(monkeypatch, capsys):
    monkeypatch.setattr("db.get_engine", lambda: (_ for _ in ()).throw(RuntimeError("missing in .env: X")))
    monkeypatch.setenv("SE_API_KEY", "k")
    assert fill.main([]) == 1
    assert "fill failed: missing in .env: X" in capsys.readouterr().err
    assert update.main([]) == 1


def test_max_requests_must_be_positive(monkeypatch, capsys):
    monkeypatch.setenv("SE_API_KEY", "k")
    assert update.main(["--max-requests", "0"]) == 1
    assert "at least 1" in capsys.readouterr().err


# --- run history and progress logging --------------------------------------------------

def test_log_lines_are_timestamped_and_flushed(capsys):
    etl_common.log("hello")
    out = capsys.readouterr().out
    import re
    assert re.fullmatch(r"\[\d\d:\d\d:\d\d\] hello\n", out)


def test_json_safe_turns_numpy_numbers_into_plain_ones():
    import numpy as np
    assert etl_common.json_safe({"a": {"b": np.int64(3)}}) == {"a": {"b": 3}}
    assert etl_common.json_safe(None) is None


def patch_job_environment(monkeypatch):
    """Make run_job run without a database; returns the list of recorded finish_run calls."""
    import load as loader

    @contextmanager
    def fake_lock(engine):
        yield

    class Engine:
        @contextmanager
        def begin(self):
            yield object()

    finished = []
    monkeypatch.setattr("db.get_engine", lambda: Engine())
    monkeypatch.setattr(etl_common, "run_lock", fake_lock)
    monkeypatch.setattr(loader, "create_schema", lambda conn: None)
    monkeypatch.setattr(loader, "start_run", lambda engine, job, budget: 7)
    monkeypatch.setattr(loader, "finish_run", lambda *a, **k: finished.append(a))
    monkeypatch.setattr(etl_common, "print_summary", lambda job, ctx: None)
    monkeypatch.setenv("SE_API_KEY", "k")
    return finished


def test_a_successful_run_is_recorded_as_ok(monkeypatch):
    finished = patch_job_environment(monkeypatch)
    args = SimpleNamespace(max_requests=5)
    assert etl_common.run_job("fill", args, lambda ctx: ctx.totals.stops.update(backfill="budget")) == 0
    engine, run_id, status, used, stops, stats, error = finished[0]
    assert (run_id, status, error) == (7, "ok", None) and stops == {"backfill": "budget"}


def test_a_failed_run_is_recorded_with_its_error(monkeypatch, capsys):
    finished = patch_job_environment(monkeypatch)

    def body(ctx):
        raise ExtractError("boom")

    assert etl_common.run_job("update", SimpleNamespace(max_requests=5), body) == 1
    assert finished[0][2] == "failed" and finished[0][6] == "ExtractError: boom"
    assert "update failed: boom" in capsys.readouterr().err


def test_losing_the_history_row_does_not_hide_the_run(monkeypatch, capsys):
    import load as loader
    ctx = make_ctx()
    ctx.client.quota_remaining = 5
    monkeypatch.setattr(loader, "finish_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    etl_common._record_finish(None, 1, "ok", None, ctx)   # must not raise
    assert "could not record the run" in capsys.readouterr().err
