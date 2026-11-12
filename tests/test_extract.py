"""Tests for extract.py. The API is mocked, so these never touch the network or a database."""
import json
from datetime import datetime, timezone

import pytest
import requests

import extract
from extract import Client, Config, ExtractError, drop_user_fields


def make_config(**overrides):
    base = dict(key="test-key", site="stackoverflow",
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc), max_pages=300,
                history_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
                tracked_tags=["python"], request_interval=0)
    return Config(**{**base, **overrides})


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def no_sleep(monkeypatch):
    """Record sleeps instead of waiting."""
    slept = []
    monkeypatch.setattr(extract.time, "sleep", slept.append)
    return slept


def script_responses(monkeypatch, responses):
    """Make requests.get return the given responses in order; returns the list of call params."""
    calls = []
    queue = iter(responses)

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(extract.requests, "get", fake_get)
    return calls


def test_drop_user_fields_strips_nested_owner():
    question = {"question_id": 1, "title": "t", "owner": {"display_name": "Real Name", "user_id": 9},
                "last_editor": {"display_name": "Other"}, "tags": ["python"]}
    assert drop_user_fields({"items": [question]}) == {"items": [{"question_id": 1, "title": "t", "tags": ["python"]}]}


def test_drop_user_fields_leaves_original_untouched():
    original = {"owner": {"user_id": 1}, "score": 3}
    drop_user_fields(original)
    assert "owner" in original


def test_paginate_stops_when_has_more_is_false(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [
        FakeResponse({"items": [{"question_id": 1}], "has_more": True, "quota_remaining": 100}),
        FakeResponse({"items": [{"question_id": 2}], "has_more": False, "quota_remaining": 99}),
    ])
    client = Client(make_config())
    items = list(client.paginate("questions", {}, "questions"))
    assert [i["question_id"] for i in items] == [1, 2]
    assert [c["page"] for c in calls] == [1, 2]
    assert client.quota_remaining == 99


def test_paginate_respects_page_cap(monkeypatch, no_sleep):
    script_responses(monkeypatch, [
        FakeResponse({"items": [{"question_id": n}], "has_more": True, "quota_remaining": 100})
        for n in range(5)])
    items = list(Client(make_config()).paginate("questions", {}, "questions", max_pages=2))
    assert len(items) == 2


def test_paginate_scrubs_user_fields(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse(
        {"items": [{"question_id": 1, "owner": {"display_name": "Real Name"}}], "has_more": False})])
    assert list(Client(make_config()).paginate("questions", {}, "questions")) == [{"question_id": 1}]


def test_raw_files_never_contain_user_fields(monkeypatch, no_sleep, tmp_path):
    script_responses(monkeypatch, [FakeResponse(
        {"items": [{"question_id": 1, "owner": {"display_name": "Real Name"}}], "has_more": False})])
    list(Client(make_config(), tmp_path).paginate("questions", {}, "questions"))
    saved = (tmp_path / "questions_p001.json").read_text(encoding="utf-8")
    assert "Real Name" not in saved and "owner" not in saved


def test_api_error_raises_with_the_api_message(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse(
        {"error_id": 400, "error_name": "bad_parameter", "error_message": "site is required"}, 400)])
    with pytest.raises(ExtractError, match="site is required"):
        Client(make_config()).fetch_page("questions", {})


def test_throttle_error_is_retried_then_succeeds(monkeypatch, no_sleep):
    script_responses(monkeypatch, [
        FakeResponse({"error_id": 502, "error_name": "throttle_violation", "error_message": "slow down"}, 400),
        FakeResponse({"items": [], "has_more": False})])
    assert Client(make_config()).fetch_page("questions", {})["items"] == []
    assert no_sleep  # backed off before retrying


def test_gives_up_after_repeated_network_errors(monkeypatch, no_sleep):
    script_responses(monkeypatch, [requests.ConnectionError("down")] * 3)
    with pytest.raises(ExtractError, match="gave up after 3 attempts"):
        Client(make_config()).fetch_page("questions", {})


def test_backoff_field_delays_the_next_request(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"items": [], "has_more": False, "backoff": 7})])
    client = Client(make_config())
    client.fetch_page("questions", {})
    assert client._next_allowed - extract.time.monotonic() > 5


def test_api_key_is_sent_when_configured_and_omitted_when_blank(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [FakeResponse({"items": []}), FakeResponse({"items": []})])
    Client(make_config(key="abc")).fetch_page("info", {})
    Client(make_config(key=None)).fetch_page("info", {})
    assert calls[0]["key"] == "abc" and "key" not in calls[1]


class StubEngine:
    """Engine whose connect() raises, like Postgres does for a missing etl_state table."""

    def __init__(self, error):
        self.error = error

    def connect(self):
        raise self.error


def test_get_watermark_returns_none_when_table_is_missing():
    from sqlalchemy.exc import ProgrammingError
    engine = StubEngine(ProgrammingError("SELECT", {}, Exception("relation etl_state does not exist")))
    assert extract.get_watermark(engine, "stackoverflow") is None


def test_get_watermark_reports_connection_failure_cleanly():
    from sqlalchemy.exc import OperationalError
    engine = StubEngine(OperationalError("SELECT", {}, Exception("connection refused")))
    with pytest.raises(ExtractError, match="could not connect to Postgres"):
        extract.get_watermark(engine, "stackoverflow")


def test_load_raw_rejects_a_folder_that_is_not_a_run(tmp_path):
    with pytest.raises(ExtractError, match="not a saved extract run"):
        extract.load_raw(tmp_path)


def test_load_raw_replays_saved_pages(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"since_epoch": 5, "max_activity_epoch": 9}))
    (tmp_path / "questions_p001.json").write_text(json.dumps({"items": [{"question_id": 1}]}))
    (tmp_path / "answers_p001.json").write_text(json.dumps({"items": [{"answer_id": 2}]}))
    result = extract.load_raw(tmp_path)
    assert result.questions == [{"question_id": 1}] and result.answers == [{"answer_id": 2}]
    assert result.since_epoch == 5
