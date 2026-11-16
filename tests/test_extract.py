"""Tests for extract.py. The API is mocked, so these never touch the network or a database."""
import json
from datetime import datetime, timezone

import pytest
import requests

import extract
from extract import Client, Config, ExtractError, drop_user_fields


def make_config(**overrides):
    base = dict(key="test-key", site="stackoverflow", max_pages=300,
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


def throttle(backoff=None):
    body = {"error_id": 502, "error_name": "throttle_violation", "error_message": "slow down"}
    if backoff:
        body["backoff"] = backoff
    return FakeResponse(body, 400)


def test_throttle_retry_waits_out_the_backoff_the_error_reports(monkeypatch, no_sleep):
    script_responses(monkeypatch, [throttle(backoff=30), FakeResponse({"items": []})])
    Client(make_config()).fetch_page("questions", {})
    assert no_sleep[-1] >= 30   # not the 2s a plain retry would use


def test_throttle_retries_are_patient_then_give_up(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [throttle()] * 10)
    with pytest.raises(ExtractError, match="gave up after 6 attempts"):
        Client(make_config()).fetch_page("questions", {})
    assert len(calls) == extract.THROTTLE_RETRIES
    assert sum(no_sleep) >= 100   # waits grow each attempt


def test_a_success_with_backoff_delays_the_next_request_with_a_margin(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"items": [], "backoff": 10})])
    client = Client(make_config())
    client.fetch_page("questions", {})
    assert client._next_allowed - extract.time.monotonic() > 10


def test_error_messages_shorten_the_long_answers_path(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"error_id": 400, "error_name": "x", "error_message": "bad"}, 400)])
    path = "questions/" + ";".join(str(n) for n in range(100)) + "/answers"
    with pytest.raises(ExtractError) as err:
        Client(make_config()).fetch_page(path, {})
    assert len(str(err.value)) < 120


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


def page(ids, has_more, quota=1000):
    return FakeResponse({"items": [{"question_id": i} for i in ids], "has_more": has_more,
                         "quota_remaining": quota})


def test_paginate_reports_why_it_stopped(monkeypatch, no_sleep):
    script_responses(monkeypatch, [page([1], False)])
    client = Client(make_config())
    list(client.paginate("questions", {}, "questions"))
    assert client.stop_reason == "done"

    script_responses(monkeypatch, [page([1], True), page([2], True)])
    client = Client(make_config())
    list(client.paginate("questions", {}, "questions", max_pages=2))
    assert client.stop_reason == "page_cap"


def test_paginate_stops_when_the_request_budget_is_spent(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [page([n], True) for n in range(10)])
    client = Client(make_config(max_requests=3))
    items = list(client.paginate("questions", {}, "questions"))
    assert len(items) == 3 and len(calls) == 3
    assert client.stop_reason == "budget"


def test_paginate_stops_before_the_first_request_when_budget_is_zero(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [])
    client = Client(make_config(max_requests=2))
    client.pages_fetched = 2
    assert list(client.paginate("questions", {}, "questions")) == []
    assert calls == [] and client.stop_reason == "budget"


def test_paginate_stops_when_quota_is_low(monkeypatch, no_sleep):
    script_responses(monkeypatch, [page([1], True, quota=5), page([2], True)])
    client = Client(make_config())
    assert len(list(client.paginate("questions", {}, "questions"))) == 1
    assert client.stop_reason == "quota"


def test_fetch_question_batch_restarts_at_page_one_with_the_given_params(monkeypatch, no_sleep):
    calls = script_responses(monkeypatch, [page([1], True), page([2], True), page([3], True)])
    client = Client(make_config())
    client.filter = "F"
    items, reason = extract.fetch_question_batch(client, {"min": 123}, pages=2)
    assert [i["question_id"] for i in items] == [1, 2] and reason == "page_cap"
    assert [c["page"] for c in calls] == [1, 2]
    assert all(c["min"] == 123 and c["filter"] == "F" for c in calls)


def test_extract_answers_reports_incomplete_when_budget_runs_out(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"items": [{"answer_id": 1}], "has_more": False})])
    client = Client(make_config(max_requests=1))
    client.filter = "F"
    answers, complete = extract.extract_answers(client, list(range(1, 201)))  # needs 2 chunks
    assert complete is False and len(answers) == 1


def test_extract_answers_complete_when_every_chunk_finishes(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"items": [{"answer_id": n}], "has_more": False})
                                   for n in (1, 2)])
    client = Client(make_config())
    client.filter = "F"
    answers, complete = extract.extract_answers(client, list(range(1, 201)))
    assert complete is True and len(answers) == 2


NOW = datetime(2026, 3, 15, tzinfo=timezone.utc)


def test_months_to_fetch_skips_complete_months_and_fetches_missing_ones():
    from datetime import date
    existing = {("site", date(2026, 1, 1)): False}
    todo = extract.months_to_fetch(existing, datetime(2026, 1, 1, tzinfo=timezone.utc), NOW, ["site"])
    assert [(s, m.strftime("%Y-%m")) for s, m, _ in todo] == [("site", "2026-02"), ("site", "2026-03")]


def test_months_to_fetch_refetches_a_partial_month_once_it_has_ended():
    from datetime import date
    existing = {("site", date(2026, 2, 1)): True, ("site", date(2026, 3, 1)): True}
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    todo = extract.months_to_fetch(existing, start, NOW, ["site"])
    assert [m.strftime("%Y-%m") for _, m, _ in todo] == ["2026-02"]   # March is still running


def test_months_to_fetch_can_always_refresh_the_current_month():
    from datetime import date
    existing = {("site", date(2026, 3, 1)): True}
    todo = extract.months_to_fetch(existing, datetime(2026, 3, 1, tzinfo=timezone.utc), NOW, ["site"],
                                   refresh_partial=True)
    assert [m.strftime("%Y-%m") for _, m, _ in todo] == ["2026-03"]


def test_extract_monthly_counts_stops_at_budget_and_marks_the_partial_month(monkeypatch, no_sleep):
    script_responses(monkeypatch, [FakeResponse({"total": 5}), FakeResponse({"total": 7})])
    client = Client(make_config(max_requests=2))
    todo = extract.months_to_fetch({}, datetime(2026, 2, 1, tzinfo=timezone.utc), NOW, ["site", "python"])
    counts, complete = extract.extract_monthly_counts(client, todo, NOW)
    assert complete is False and client.stop_reason == "budget"
    assert [(c["scope"], c["month"], c["is_partial_month"]) for c in counts] == [
        ("site", "2026-02", False), ("python", "2026-02", False)]


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
