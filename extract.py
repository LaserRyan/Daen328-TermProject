"""Extract: pull questions, answers, and monthly question counts from the Stack Exchange API.

Privacy (Phase 1 risk): no user or account data is requested, saved to disk, or returned.
A custom API filter whitelists the fields we need, and drop_user_fields() scrubs every
item again before it is written or handed to transform.

Run standalone:
    python extract.py --no-db --since 2026-11-10 --skip-counts
    python extract.py --from-raw data/raw/<run_stamp>
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

API = "https://api.stackexchange.com/2.3"
RAW_ROOT = Path(__file__).resolve().parent / "data" / "raw"
OVERLAP_SECONDS = 300
MAX_RETRIES = 3
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
    start_date: datetime
    max_pages: int
    history_start: datetime
    tracked_tags: list[str]
    request_interval: float


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
    start = os.getenv("SE_START_DATE", "").strip()
    try:
        max_pages = int(os.getenv("SE_MAX_PAGES", "300"))
        interval = float(os.getenv("SE_REQUEST_INTERVAL", "0.25"))
    except ValueError as e:
        raise ExtractError(f"Bad number in .env: {e}")
    tags = os.getenv("SE_TRACKED_TAGS", "python;javascript;java;c#;c++;reactjs;sql;typescript")
    return Config(
        key=os.getenv("SE_API_KEY", "").strip() or None,
        site=os.getenv("SE_SITE", "stackoverflow").strip(),
        start_date=(_parse_date(start, "SE_START_DATE") if start
                    else datetime.now(timezone.utc) - timedelta(days=60)),
        max_pages=max_pages,
        history_start=_parse_date(os.getenv("SE_HISTORY_START", "2020-01-01"), "SE_HISTORY_START"),
        tracked_tags=[t.strip() for t in tags.split(";") if t.strip()],
        request_interval=interval,
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
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_turn()
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
                        raise ExtractError(f"{path}: {retry_reason}")
                elif "error_id" in data:
                    retry_reason = (f"API error {data['error_id']} {data.get('error_name', '')}: "
                                    f"{data.get('error_message', '')}")
                    if data["error_id"] != 502:  # 502 = throttle_violation, worth retrying
                        raise ExtractError(f"{path}: {retry_reason}")
                elif resp.status_code >= 500:
                    retry_reason = f"HTTP {resp.status_code}"
                else:
                    self.pages_fetched += 1
                    self.quota_remaining = data.get("quota_remaining", self.quota_remaining)
                    if data.get("backoff"):
                        self._next_allowed = time.monotonic() + data["backoff"]
                    return data
            if attempt == MAX_RETRIES:
                raise ExtractError(f"{path}: gave up after {MAX_RETRIES} attempts, last was {retry_reason}")
            wait = 2 ** attempt
            print(f"  retrying {path} in {wait}s ({retry_reason})", flush=True)
            time.sleep(wait)

    def quota_low(self):
        return self.quota_remaining is not None and self.quota_remaining < QUOTA_FLOOR

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
        """Yield scrubbed items page by page until has_more is false, the page cap, or low quota."""
        page = 1
        while True:
            data = drop_user_fields(self.fetch_page(path, {**params, "page": page, "pagesize": PAGE_SIZE}))
            self.save_raw(kind, data)
            yield from data.get("items", [])
            if not data.get("has_more"):
                return
            if max_pages and page >= max_pages:
                print(f"  {kind}: stopped at the page cap ({max_pages}); the next run continues from the watermark")
                return
            if self.quota_low():
                print(f"  {kind}: stopping early, only {self.quota_remaining} requests of quota left")
                return
            page += 1


def extract_questions(client, min_epoch):
    params = {"sort": "activity", "order": "asc", "min": min_epoch, "filter": client.filter}
    return list(client.paginate("questions", params, "questions", client.config.max_pages))


def extract_answers(client, question_ids):
    answers = []
    for i in range(0, len(question_ids), 100):
        if client.quota_low():
            print(f"  answers: stopping early, only {client.quota_remaining} requests of quota left")
            break
        ids = ";".join(str(q) for q in question_ids[i:i + 100])
        params = {"sort": "creation", "order": "asc", "filter": client.filter}
        answers += client.paginate(f"questions/{ids}/answers", params, "answers")
    return answers


def _months(start, end):
    month = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    while month <= end:
        nxt = datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=timezone.utc)
        yield month, nxt
        month = nxt


def extract_monthly_counts(client, since_month):
    """Question counts per month, site-wide and per tracked tag, one request each (filter=total)."""
    counts = []
    now = datetime.now(timezone.utc)
    for month, nxt in _months(since_month, now):
        for tag in [None] + client.config.tracked_tags:
            if client.quota_low():
                print(f"  monthly counts: stopping early, only {client.quota_remaining} requests of quota left")
                client.save_raw("counts", counts)
                return counts
            params = {"fromdate": int(month.timestamp()), "todate": int(nxt.timestamp()) - 1,
                      "filter": "total"}
            if tag:
                params["tagged"] = tag
            data = client.fetch_page("questions", params)
            counts.append({"scope": tag or "site", "month": month.strftime("%Y-%m"),
                           "question_count": data.get("total"), "is_partial_month": nxt > now})
    client.save_raw("counts", counts)
    return counts


# --- watermark, stored in Postgres ---------------------------------------------------

def state_key(site):
    return f"{site}:questions"


def get_watermark(engine, site):
    """Latest last_activity_date already loaded, or None on a first run."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT last_activity_epoch FROM etl_state WHERE source = :s"),
                               {"s": state_key(site)}).fetchone()
    except ProgrammingError:
        return None  # etl_state not created yet: first run
    except OperationalError as e:
        raise ExtractError(f"could not connect to Postgres ({str(e.orig).strip().splitlines()[0]})")
    return row[0] if row else None


def save_watermark(engine, site, epoch):
    """Call only after load has committed, so a failed load is retried on the next run."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS etl_state (
                source VARCHAR(100) PRIMARY KEY,
                last_activity_epoch BIGINT NOT NULL,
                last_run_at TIMESTAMPTZ NOT NULL)"""))
        conn.execute(text("""
            INSERT INTO etl_state (source, last_activity_epoch, last_run_at)
            VALUES (:s, :e, now())
            ON CONFLICT (source) DO UPDATE
            SET last_activity_epoch = EXCLUDED.last_activity_epoch,
                last_run_at = EXCLUDED.last_run_at"""),
            {"s": state_key(site), "e": epoch})


# --- orchestration --------------------------------------------------------------------

def extract(engine=None, since=None, skip_counts=False, config=None):
    """Run every stream. `since` (a datetime) overrides the stored watermark."""
    config = config or load_config()
    if not config.key:
        print("  warning: SE_API_KEY is blank, so requests are anonymous (300/day). "
              "See 'Stack Exchange API Instructions/stack-exchange-api-key-tutorial.md'.")

    if since is not None:
        since_epoch = int(since.timestamp())
    else:
        watermark = get_watermark(engine, config.site) if engine is not None else None
        since_epoch = watermark - OVERLAP_SECONDS if watermark is not None else None
    first_run = since_epoch is None
    min_epoch = int(config.start_date.timestamp()) if first_run else since_epoch

    raw_dir = RAW_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = Client(config, raw_dir)
    client.build_filter()

    questions = extract_questions(client, min_epoch)
    answered = sorted({q["question_id"] for q in questions if q.get("answer_count")})
    answers = extract_answers(client, answered)

    counts = []
    if not skip_counts:
        if first_run:
            counts_from = config.history_start
        else:  # refresh the previous and current month only
            this_month = datetime.fromtimestamp(since_epoch, timezone.utc).replace(day=1)
            counts_from = (this_month - timedelta(days=1)).replace(day=1)
        counts = extract_monthly_counts(client, counts_from)

    result = ExtractResult(
        questions=questions, answers=answers, monthly_counts=counts,
        since_epoch=since_epoch,
        max_activity_epoch=max((q["last_activity_date"] for q in questions), default=None),
        pages_fetched=client.pages_fetched, quota_remaining=client.quota_remaining,
        raw_dir=str(raw_dir),
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "run.json").write_text(json.dumps({
        "site": config.site, "since_epoch": since_epoch, "min_epoch": min_epoch,
        "max_activity_epoch": result.max_activity_epoch, "pages_fetched": result.pages_fetched,
        "quota_remaining": result.quota_remaining,
    }, indent=2), encoding="utf-8")
    return result


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract Stack Exchange data (no user data).")
    parser.add_argument("--since", help="YYYY-MM-DD; overrides the watermark stored in Postgres")
    parser.add_argument("--from-raw", help="replay a saved run from data/raw/<stamp> without calling the API")
    parser.add_argument("--no-db", action="store_true", help="don't read the watermark from Postgres")
    parser.add_argument("--skip-counts", action="store_true", help="skip the monthly counts stream")
    args = parser.parse_args(argv)
    try:
        if args.from_raw:
            result = load_raw(args.from_raw)
        else:
            engine = None
            if not args.no_db:
                from db import get_engine
                engine = get_engine()
            since = _parse_date(args.since, "--since") if args.since else None
            result = extract(engine, since=since, skip_counts=args.skip_counts)
    except (ExtractError, RuntimeError) as e:
        print(f"Extract failed: {e}", file=sys.stderr)
        return 1
    for line in summary_lines(result):
        print("  " + line)
    print(f"  raw files: {result.raw_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
