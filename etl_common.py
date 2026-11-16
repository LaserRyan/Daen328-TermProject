"""Shared run logic for the two pipeline jobs.

    fill.py     only requests data we don't have yet: missing monthly counts, and questions
                older than the backfill cursor (walks backward by creation date).
    update.py   finds questions with activity since the watermark and upserts them
                (walks forward by last_activity_date), and refreshes the current month's counts.

Both jobs call run_job(), which:
  1. loads the config and connects to Postgres (db.py: Neon via DATABASE_URL, or local),
  2. takes a database-wide advisory lock, so two runs can never write at once,
  3. creates the schema if needed,
  4. runs the job's streams, each in batches: extract -> answers -> transform -> load, with the
     cursor written in the same transaction as the batch's rows,
  5. prints one summary and exits 0, or one error line on stderr and exits 1.

Each run spends at most SE_MAX_REQUESTS API calls (a shared team quota: whatever one person
leaves undone, the next person's run picks up from the cursors stored in the database).
"""
import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import load as loader
import transform as transformer
from extract import (
    OVERLAP_SECONDS, RAW_ROOT, Client, ExtractError, ExtractResult, extract_answers,
    extract_monthly_counts, fetch_question_batch, load_config, months_to_fetch,
)

# Any fixed number works; every run uses the same one, so a second run sees the lock is taken.
LOCK_KEY = 328_000_001

STOP_TEXT = {
    "done": "finished, nothing more to fetch right now",
    "floor": "reached SE_BACKFILL_FLOOR, nothing left to fill",
    "page_cap": "stopped at the page cap; the next run continues",
    "max_pages": "stopped at SE_MAX_PAGES; the next run continues",
    "budget": "request budget (SE_MAX_REQUESTS) spent; the next run continues",
    "quota": "API quota nearly used up; the next run continues",
    "stalled": "stopped: the cursor could not move forward (too many items share one timestamp)",
    "nothing": "nothing to fetch",
}


def log(message):
    """One timestamped progress line, flushed so `docker compose logs -f` shows it live."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def json_safe(tree):
    """Round-trip through JSON so numpy numbers and the like become plain values for JSONB."""
    return json.loads(json.dumps(tree, default=int))


def merge_stats(a, b):
    """Add two stats trees (dicts of dicts of numbers) leaf by leaf."""
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, dict):
        return {k: merge_stats(a.get(k), b.get(k)) for k in {**a, **b}}
    return a + b


class RunTotals:
    """Transform and load stats added up across all batches of one run."""

    def __init__(self):
        self.transform = None
        self.load = None
        self.stops = {}      # stream name -> stop reason code
        self.discarded = 0   # batches dropped because their answers couldn't all be fetched

    def add(self, transform_stats, load_stats):
        self.transform = merge_stats(self.transform, transform_stats)
        self.load = merge_stats(self.load, load_stats)


@dataclass
class Ctx:
    config: object
    engine: object
    client: object
    totals: RunTotals


# --- locking ---------------------------------------------------------------------------

@contextmanager
def run_lock(engine):
    """Hold a Postgres advisory lock for the whole run, or raise ExtractError if it is taken.

    The lock lives on its own autocommit connection (it is released if the process dies).
    Use Neon's direct connection string: a pooled one can hand the lock to another session.
    """
    try:
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    except OperationalError as e:
        raise ExtractError(f"could not connect to Postgres ({str(e.orig).strip().splitlines()[0]})")
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            raise ExtractError("another run is in progress against this database; wait for it to finish")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        conn.close()


# --- streams ---------------------------------------------------------------------------

def _budget_text(ctx):
    client = ctx.client
    return (f"requests {client.pages_fetched:,}/{ctx.config.max_requests:,}, "
            f"API quota left {client.quota_remaining if client.quota_remaining is not None else '?'}")


def _process_batch(ctx, questions, state):
    """Answers -> transform -> load for one batch of questions. False if answers were cut short.

    A batch with incomplete answers is not loaded and its cursor is not moved, so a question is
    never stored without its answers; the next run fetches it again.
    """
    answered = sorted({q["question_id"] for q in questions if q.get("answer_count")})
    answers, complete = extract_answers(ctx.client, answered)
    if not complete:
        ctx.totals.discarded += 1
        return False
    cleaned = transformer.transform(ExtractResult(questions=questions, answers=answers))
    ctx.totals.add(cleaned.stats, loader.load(ctx.engine, cleaned, state=state))
    stream = state[0][0].split(":")[-1] if state else "batch"
    where = f"; cursor now {_day(state[0][1])}" if state else ""
    log(f"{stream}: loaded {len(cleaned.questions):,} questions + {len(cleaned.answers):,} answers{where}"
        f" ({_budget_text(ctx)})")
    return True


def run_counts(ctx, refresh_partial=False, recent_only=False):
    """Monthly counts. fill: every missing month. update: the previous and current month only."""
    now = datetime.now(timezone.utc)
    start = ctx.config.history_start
    if recent_only:
        this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = max(start, (this_month - timedelta(days=1)).replace(day=1))
    with ctx.engine.connect() as conn:
        existing = loader.existing_counts(conn)
    todo = months_to_fetch(existing, start, now, ["site"] + ctx.config.tracked_tags, refresh_partial)
    if not todo:
        ctx.totals.stops["monthly counts"] = "nothing"
        return
    log(f"monthly counts: {len(todo):,} to fetch (one request each)")
    counts, complete = extract_monthly_counts(ctx.client, todo, now)
    ctx.totals.stops["monthly counts"] = "done" if complete else ctx.client.stop_reason
    if counts:
        cleaned = transformer.transform(ExtractResult(monthly_counts=counts))
        ctx.totals.add(cleaned.stats, loader.load(ctx.engine, cleaned))
        log(f"monthly counts: loaded {len(counts):,} ({_budget_text(ctx)})")


def run_forward(ctx):
    """Questions with activity since the watermark, oldest activity first."""
    cfg, client = ctx.config, ctx.client
    source = loader.forward_source(cfg.site)
    with ctx.engine.connect() as conn:
        watermark = loader.get_state(conn, source)
    if watermark is None:
        raise ExtractError("nothing to update yet: run fill first (it sets the starting point)")
    cursor = watermark - OVERLAP_SECONDS
    pages_left = cfg.max_pages
    while True:
        before = client.pages_fetched
        questions, reason = fetch_question_batch(
            client, {"sort": "activity", "order": "asc", "min": cursor},
            min(cfg.batch_pages, pages_left))
        pages_left -= client.pages_fetched - before
        newest = max((q["last_activity_date"] for q in questions), default=None)
        if questions and not _process_batch(ctx, questions, [(source, newest, "forward")]):
            reason = client.stop_reason
            break
        if reason != "page_cap":
            break
        if newest is None or newest <= cursor:
            reason = "stalled"
            break
        if pages_left <= 0:
            reason = "max_pages"
            break
        cursor = newest   # next batch starts here (inclusive, so ties are re-fetched, not skipped)
    ctx.totals.stops["updated questions"] = reason


def run_backfill(ctx):
    """Questions created before the backfill cursor, newest first, down to SE_BACKFILL_FLOOR."""
    cfg, client = ctx.config, ctx.client
    source = loader.backfill_source(cfg.site)
    floor = int(cfg.backfill_floor.timestamp())
    with ctx.engine.connect() as conn:
        cursor = loader.get_state(conn, source)
    if cursor is None:
        raise ExtractError("backfill cursor is missing (it is set at the start of every fill run)")
    if cursor <= floor:
        ctx.totals.stops["backfill"] = "floor"
        return
    while True:
        questions, reason = fetch_question_batch(
            client, {"sort": "creation", "order": "desc", "fromdate": floor, "todate": cursor},
            cfg.batch_pages)
        oldest = min((q["creation_date"] for q in questions), default=None)
        new_cursor = floor if reason == "done" else oldest   # "done": everything down to the floor is in
        if questions:
            if not _process_batch(ctx, questions, [(source, new_cursor, "backward")]):
                reason = client.stop_reason
                break
        elif reason == "done":
            with ctx.engine.begin() as conn:
                loader.set_state(conn, source, floor, "backward")
        if reason == "done":
            reason = "floor"
            break
        if reason != "page_cap":
            break
        if oldest >= cursor:
            reason = "stalled"
            break
        cursor = oldest
    ctx.totals.stops["backfill"] = reason


def ensure_seeded(ctx):
    """First fill run: start both cursors at "now". Existing cursors are never changed.

    fill walks backward from this moment and update walks forward from it, so together they
    cover everything with no gap. One /info request first proves the site name and key are
    valid, so a typo can't leave cursor rows behind. Costs no API call once seeded.
    """
    site = ctx.config.site
    with ctx.engine.connect() as conn:
        if loader.get_state(conn, loader.backfill_source(site)) is not None:
            return
    ctx.client.fetch_page("info", {})
    now = int(datetime.now(timezone.utc).timestamp())
    with ctx.engine.begin() as conn:
        loader.seed_state(conn, loader.backfill_source(site), now)
        loader.seed_state(conn, loader.forward_source(site), now)
    log(f"first run for {site}: cursors start at {_day(now)}")


# --- running a job ---------------------------------------------------------------------

def make_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--max-requests", type=int,
                        help="API calls this run may spend (default: SE_MAX_REQUESTS)")
    return parser


def _fmt_epoch(epoch):
    return "not set" if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def print_summary(job, ctx):
    client, totals = ctx.client, ctx.totals
    print(f"{job} summary")
    print(f"  API requests used: {client.pages_fetched:,} of {ctx.config.max_requests:,} "
          f"(API quota remaining: {client.quota_remaining})")
    for stream, reason in totals.stops.items():
        print(f"  {stream}: {STOP_TEXT.get(reason, reason)}")
    if totals.discarded:
        print("  last batch dropped, its answers could not all be fetched; it is retried next run")
    if totals.transform:
        for line in transformer.summary_lines(totals.transform):
            print("  " + line)
        for line in loader.summary_lines(totals.load):
            print("  " + line)
    with ctx.engine.connect() as conn:
        forward = loader.get_state(conn, loader.forward_source(ctx.config.site))
        backfill = loader.get_state(conn, loader.backfill_source(ctx.config.site))
    print(f"  cursors: update continues from {_fmt_epoch(forward)}; "
          f"fill continues before {_fmt_epoch(backfill)}")


def _write_run_json(job, ctx, raw_dir):
    if not raw_dir.exists():   # nothing was fetched, so there is nothing to replay
        return
    (raw_dir / "run.json").write_text(json.dumps({
        "job": job, "site": ctx.config.site, "since_epoch": None, "max_activity_epoch": None,
        "pages_fetched": ctx.client.pages_fetched, "quota_remaining": ctx.client.quota_remaining,
        "stops": ctx.totals.stops,
    }, indent=2), encoding="utf-8")


def _record_finish(engine, run_id, status, error, ctx):
    """Close the run's etl_runs row. Never raises: losing the history row must not hide the real error."""
    try:
        loader.finish_run(engine, run_id, status, ctx.client.pages_fetched,
                          json_safe(ctx.totals.stops), json_safe(ctx.totals.load), error)
    except Exception as e:
        print(f"  warning: could not record the run in etl_runs ({e.__class__.__name__})", file=sys.stderr)


def run_job(job, args, body):
    """Run `body(ctx)` under the lock. Returns the process exit code."""
    from db import get_engine
    try:
        config = load_config()
        if args.max_requests is not None:
            if args.max_requests < 1:
                raise ExtractError("--max-requests must be at least 1")
            config.max_requests = args.max_requests
        if not config.key:
            print("  warning: SE_API_KEY is blank, so requests are anonymous (300/day). "
                  "See docs/stack-exchange-api-key-tutorial.md.")
        engine = get_engine()
        with run_lock(engine):
            with engine.begin() as conn:
                loader.create_schema(conn)
            raw_dir = RAW_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ctx = Ctx(config, engine, Client(config, raw_dir), RunTotals())
            run_id = loader.start_run(engine, job, config.max_requests)
            log(f"{job} run #{run_id} started: budget {config.max_requests:,} requests, "
                f"site {config.site}")
            status, error = "ok", None
            try:
                body(ctx)
            except BaseException as e:   # includes Ctrl-C: record it, then let it propagate
                status, error = "failed", f"{type(e).__name__}: {e}"
                raise
            finally:
                _write_run_json(job, ctx, raw_dir)
                _record_finish(engine, run_id, status, error, ctx)
            print_summary(job, ctx)
    except (ExtractError, RuntimeError) as e:
        print(f"{job} failed: {e}", file=sys.stderr)
        return 1
    return 0
