"""Extract: pull questions, answers, and monthly question counts from the Stack Exchange API.

This module only talks to the API. It never touches the database: where a run starts and
stops (the cursors in etl_state) is handled by fill.py, update.py and etl_common.py.

Privacy (Phase 1 risk): no user or account data is requested, saved to disk, or returned.
A custom API filter whitelists the fields we need, and drop_user_fields() scrubs every
item again before it is written or handed to transform.

Every Client has a request budget (SE_MAX_REQUESTS). paginate() and the stream helpers stop
cleanly when it is spent or the API's own quota_remaining runs low, and record why in
Client.stop_reason ("done", "page_cap", "budget" or "quota").

Run standalone:
    python extract.py --since 2026-11-10          # quick check: one page, no database
    python extract.py --from-raw data/raw/<run_stamp>
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

API = "https://api.stackexchange.com/2.3"
RAW_ROOT = Path(__file__).resolve().parent / "data" / "raw"
OVERLAP_SECONDS = 300
MAX_RETRIES = 3
THROTTLE_RETRIES = 6    # throttle violations are worth waiting out for much longer
BACKOFF_MARGIN = 1      # extra seconds on top of every API backoff
QUOTA_FLOOR = 10
PAGE_SIZE = 100

# Fields we keep. Anything not listed (owner, last_editor, body, comments) is never returned.
FILTER_INCLUDE = [
    "question.question_id", "question.title", "question.tags", "question.score",
    "question.view_count", "question.answer_count", "question.comment_count",
    "question.is_answered", "question.accepted_answer_id", "question.creation_date",
    "question.last_activity_date", "question.last_edit_date", "question.closed_date",
    "question.closed_reason", "question.link",
    "answer.answer_id", "answer.question_id", "answer.score", "answer.is_accepted",
    "answer.creation_date", "answer.last_activity_date", "answer.last_edit_date",
    ".items", ".has_more", ".quota_max", ".quota_remaining", ".backoff", ".total",
    ".error_id", ".error_name", ".error_message",
]
USER_FIELDS = {"owner", "last_editor", "user_id", "account_id", "display_name",
               "profile_image", "reply_to_user"}


class ExtractError(Exception):
    """Any failure the pipeline should report as one clear line."""


@dataclass
class Config:
    key: str | None
    site: str
    max_pages: int              # cap on question pages per update run
    history_start: datetime     # monthly counts start here
    tracked_tags: list[str]
    request_interval: float
    max_requests: int = 10000   # API calls one run may spend (our per-person daily budget)
    backfill_floor: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)  # fill stops here
    batch_pages: int = 10       # question pages per committed batch


@dataclass
class ExtractResult:
    questions: list[dict] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)
    monthly_counts: list[dict] = field(default_factory=list)
    since_epoch: int | None = None
    max_activity_epoch: int | None = None
    pages_fetched: int = 0
    quota_remaining: int | None = None
    raw_dir: str | None = None


def _parse_date(value, name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ExtractError(f"{name} must be YYYY-MM-DD, got {value!r}")


def load_config():
    load_dotenv()
    try:
        max_pages = int(os.getenv("SE_MAX_PAGES", "300"))
        interval = float(os.getenv("SE_REQUEST_INTERVAL", "0.25"))
        max_requests = int(os.getenv("SE_MAX_REQUESTS", "10000"))
        batch_pages = int(os.getenv("SE_BATCH_PAGES", "10"))
    except ValueError as e:
        raise ExtractError(f"Bad number in .env: {e}")
    if max_requests < 1 or batch_pages < 1 or max_pages < 1:
        raise ExtractError("SE_MAX_REQUESTS, SE_BATCH_PAGES and SE_MAX_PAGES must be at least 1")
    tags = os.getenv("SE_TRACKED_TAGS", "python;javascript;java;c#;c++;reactjs;sql;typescript")
    return Config(
        key=os.getenv("SE_API_KEY", "").strip() or None,
        site=os.getenv("SE_SITE", "stackoverflow").strip(),
        max_pages=max_pages,
        history_start=_parse_date(os.getenv("SE_HISTORY_START", "2020-01-01"), "SE_HISTORY_START"),
        tracked_tags=[t.strip().lower() for t in tags.split(";") if t.strip()],
        request_interval=interval,
        max_requests=max_requests,
        backfill_floor=_parse_date(os.getenv("SE_BACKFILL_FLOOR", "2025-01-01"), "SE_BACKFILL_FLOOR"),
        batch_pages=batch_pages,
    )


def drop_user_fields(obj):
    """Return a copy of obj with every user/account field removed, at any depth."""
    if isinstance(obj, dict):
        return {k: drop_user_fields(v) for k, v in obj.items() if k not in USER_FIELDS}
    if isinstance(obj, list):
        return [drop_user_fields(v) for v in obj]
    return obj


class Client:
    """One Stack Exchange session: pacing, retries, backoff, quota, and raw saves."""

    def __init__(self, config, raw_dir=None):
        self.config = config
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.pages_fetched = 0
        self.quota_remaining = None
        self.filter = None
        self.stop_reason = None   # why the last paginate()/stream helper stopped
        self._next_allowed = 0.0
        self._saved = {}

    def _wait_turn(self):
        delay = self._next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def fetch_page(self, path, params):
        """One API call. Returns parsed JSON or raises ExtractError."""
        if path != "filters/create":  # the only endpoint that rejects `site`
            params = {"site": self.config.site, **params}
        if self.config.key:
            params["key"] = self.config.key
        url = f"{API}/{path}"
        # Answers URLs carry up to 100 ids; keep error messages readable.
        label = path if len(path) <= 40 else path[:20] + "..." + path.rsplit("/", 1)[-1]
        attempt, throttled = 0, False
        while True:
            attempt += 1
            self._wait_turn()
            backoff = 0
            throttled = False
            try:
                resp = requests.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                retry_reason = f"network error ({e.__class__.__name__})"
            else:
                self._next_allowed = time.monotonic() + self.config.request_interval
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if data is None:
                    retry_reason = f"HTTP {resp.status_code} with a non-JSON body"
                    if resp.status_code < 500:
                        raise ExtractError(f"{label}: {retry_reason}")
                elif "error_id" in data:
                    retry_reason = (f"API error {data['error_id']} {data.get('error_name', '')}: "
                                    f"{data.get('error_message', '')}")
                    if data["error_id"] != 502:  # 502 = throttle_violation, worth retrying
                        raise ExtractError(f"{label}: {retry_reason}")
                    throttled = True
                    backoff = data.get("backoff") or 0
                elif resp.status_code >= 500:
                    retry_reason = f"HTTP {resp.status_code}"
                else:
                    self.pages_fetched += 1
                    self.quota_remaining = data.get("quota_remaining", self.quota_remaining)
                    if data.get("backoff"):
                        # +1s margin: the API counts the backoff from when it answered.
                        self._next_allowed = time.monotonic() + data["backoff"] + BACKOFF_MARGIN
                    return data
            limit = THROTTLE_RETRIES if throttled else MAX_RETRIES
            if attempt >= limit:
                raise ExtractError(f"{label}: gave up after {attempt} attempts, last was {retry_reason}")
            # A throttle violation means we asked too soon: wait out the API's backoff (at least
            # 10s), longer each time. Anything else gets a short exponential wait.
            wait = max(backoff, 10) * attempt + BACKOFF_MARGIN if throttled else 2 ** attempt
            self._next_allowed = max(self._next_allowed, time.monotonic() + wait)
            print(f"  retrying {label} in {wait}s ({retry_reason})", flush=True)
            time.sleep(wait)

    def quota_low(self):
        return self.quota_remaining is not None and self.quota_remaining < QUOTA_FLOOR

    def budget_left(self):
        return self.config.max_requests - self.pages_fetched

    def spend_block(self):
        """Why the next request must not be sent ("budget" or "quota"), or None if it may."""
        if self.budget_left() <= 0:
            return "budget"
        if self.quota_low():
            return "quota"
        return None

    def can_spend(self):
        return self.spend_block() is None

    def ensure_filter(self):
        return self.filter or self.build_filter()

    def save_raw(self, kind, payload):
        if not self.raw_dir:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        n = self._saved[kind] = self._saved.get(kind, 0) + 1
        (self.raw_dir / f"{kind}_p{n:03d}.json").write_text(json.dumps(payload), encoding="utf-8")

    def build_filter(self):
        data = self.fetch_page("filters/create", {
            "include": ";".join(FILTER_INCLUDE), "base": "none", "unsafe": "false"})
        try:
            self.filter = data["items"][0]["filter"]
        except (KeyError, IndexError):
            raise ExtractError("filters/create returned no filter")
        return self.filter

    def paginate(self, path, params, kind, max_pages=None):
        """Yield scrubbed items page by page. Sets self.stop_reason when it ends:

        "done"      the API said has_more is false: nothing more to fetch
        "page_cap"  max_pages reached and the API has more (the caller can continue)
        "budget"    SE_MAX_REQUESTS spent
        "quota"     the API's quota_remaining is below the floor
        """
        page = 1
        while True:
            blocked = self.spend_block()
            if blocked:
                self.stop_reason = blocked
                return
            data = drop_user_fields(self.fetch_page(path, {**params, "page": page, "pagesize": PAGE_SIZE}))
            self.save_raw(kind, data)
            yield from data.get("items", [])
            if not data.get("has_more"):
                self.stop_reason = "done"
                return
            if max_pages and page >= max_pages:
                self.stop_reason = "page_cap"
                return
            page += 1


def fetch_question_batch(client, params, pages):
    """Up to `pages` pages of questions from page 1. Returns (items, stop_reason).

    stop_reason "page_cap" means the API has more beyond this batch; anything else means the
    stream ended (done) or ran out of budget/quota. Pages arrive in sort order, so a batch cut
    short by budget or quota is still a contiguous prefix and safe to load.
    """
    blocked = client.spend_block()   # building the filter costs a request too
    if blocked:
        client.stop_reason = blocked
        return [], blocked
    items = list(client.paginate("questions", {**params, "filter": client.ensure_filter()},
                                 "questions", max_pages=pages))
    return items, client.stop_reason


def extract_answers(client, question_ids):
    """Answers for the given questions. Returns (answers, complete).

    complete is False if the budget or quota ran out first. The caller must then not load
    the batch, so no question is stored without its answers.
    """
    answers = []
    for i in range(0, len(question_ids), 100):
        blocked = client.spend_block()
        if blocked:
            client.stop_reason = blocked
            return answers, False
        ids = ";".join(str(q) for q in question_ids[i:i + 100])
        params = {"sort": "creation", "order": "asc", "filter": client.ensure_filter()}
        answers += client.paginate(f"questions/{ids}/answers", params, "answers")
        if client.stop_reason != "done":
            return answers, False
    return answers, True


def _months(start, end):
    month = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    while month <= end:
        nxt = datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=timezone.utc)
        yield month, nxt
        month = nxt


def months_to_fetch(existing, start, now, scopes, refresh_partial=False):
    """Which (scope, month_start, next_month_start) counts still need an API request.

    existing maps (scope, date of first of month) -> is_partial_month for rows already stored.
    A pair is fetched if it is not stored. A stored partial month is fetched again once that
    month has ended, or always when refresh_partial is True (update refreshes the current month).
    Months already stored as complete are never fetched again.
    """
    todo = []
    for month, nxt in _months(start, now):
        for scope in scopes:
            stored = existing.get((scope, month.date()))
            if stored is None or (stored and (refresh_partial or nxt <= now)):
                todo.append((scope, month, nxt))
    return todo


def extract_monthly_counts(client, todo, now=None):
    """Question counts for each (scope, month, next_month) in todo, one request each.

    Returns (counts, complete); complete is False if budget or quota ran out first.
    """
    now = now or datetime.now(timezone.utc)
    counts = []
    complete = True
    for scope, month, nxt in todo:
        blocked = client.spend_block()
        if blocked:
            client.stop_reason = blocked
            complete = False
            break
        params = {"fromdate": int(month.timestamp()), "todate": int(nxt.timestamp()) - 1,
                  "filter": "total"}
        if scope != "site":
            params["tagged"] = scope
        data = client.fetch_page("questions", params)
        counts.append({"scope": scope, "month": month.strftime("%Y-%m"),
                       "question_count": data.get("total"), "is_partial_month": nxt > now})
    if complete:
        client.stop_reason = "done"
    if counts:
        client.save_raw("counts", counts)
    return counts, complete


def load_raw(raw_dir):
    """Rebuild an ExtractResult from a saved run without calling the API."""
    raw_dir = Path(raw_dir)
    if not (raw_dir / "run.json").exists():
        raise ExtractError(f"{raw_dir} is not a saved extract run (no run.json)")
    meta = json.loads((raw_dir / "run.json").read_text(encoding="utf-8"))

    def read(kind):
        return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(raw_dir.glob(f"{kind}_p*.json"))]

    return ExtractResult(
        questions=[i for page in read("questions") for i in page.get("items", [])],
        answers=[i for page in read("answers") for i in page.get("items", [])],
        monthly_counts=[c for page in read("counts") for c in page],
        since_epoch=meta.get("since_epoch"), max_activity_epoch=meta.get("max_activity_epoch"),
        pages_fetched=0, quota_remaining=meta.get("quota_remaining"), raw_dir=str(raw_dir),
    )


def summary_lines(result):
    if result.since_epoch is None:
        since = "full starting dataset"
    else:
        since = "incremental since " + datetime.fromtimestamp(
            result.since_epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        f"received from API: {len(result.questions):,} questions, {len(result.answers):,} answers, "
        f"{len(result.monthly_counts):,} monthly counts ({since})",
        f"API pages fetched: {result.pages_fetched:,}, quota remaining: {result.quota_remaining}",
    ]


def quick_check(since):
    """One page of questions active since `since`, no database. A fast way to test a key."""
    config = load_config()
    client = Client(config, RAW_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    params = {"sort": "activity", "order": "asc", "min": int(since.timestamp())}
    questions, reason = fetch_question_batch(client, params, 1)
    answered = sorted({q["question_id"] for q in questions if q.get("answer_count")})
    answers, _ = extract_answers(client, answered)
    return ExtractResult(questions=questions, answers=answers, since_epoch=int(since.timestamp()),
                         pages_fetched=client.pages_fetched, quota_remaining=client.quota_remaining,
                         raw_dir=str(client.raw_dir))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract Stack Exchange data (no user data). "
                                     "The full pipeline is fill.py and update.py.")
    parser.add_argument("--since", help="YYYY-MM-DD: quick check, fetch one page of questions active since then")
    parser.add_argument("--from-raw", help="replay a saved run from data/raw/<stamp> without calling the API")
    args = parser.parse_args(argv)
    try:
        if args.from_raw:
            result = load_raw(args.from_raw)
        elif args.since:
            result = quick_check(_parse_date(args.since, "--since"))
        else:
            parser.error("give --since YYYY-MM-DD or --from-raw <dir>")
    except (ExtractError, RuntimeError) as e:
        print(f"Extract failed: {e}", file=sys.stderr)
        return 1
    for line in summary_lines(result):
        print("  " + line)
    print(f"  raw files: {result.raw_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
