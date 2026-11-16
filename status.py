"""Status: what is in the database and what the pipeline has been doing. Read-only.

    python status.py                     # against whichever database .env points at
    python status.py --runs 20           # show more run history
    docker compose run --rm status

Shows: whether a run is active right now, both cursors, row counts, how far back the data
reaches, and the most recent fill/update runs (budget used, why each stopped, errors).
"""
import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from etl_common import LOCK_KEY

TABLES = ["questions", "answers", "tags", "question_tags", "tag_monthly_counts"]


def fmt_epoch(epoch):
    return "not set" if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_time(ts):
    return "-" if ts is None else ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_duration(start, end):
    if start is None or end is None:
        return "-"
    seconds = int((end - start).total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else (f"{minutes}m{secs:02d}s" if minutes else f"{secs}s")


def run_is_active(conn):
    """True if another session holds the pipeline lock, i.e. a fill/update is running right now."""
    got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
    if got:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    return not got


def describe_run(row, active_now):
    """One line for an etl_runs row. A 'running' row with no live run was interrupted."""
    status = row["status"]
    if status == "running" and not active_now:
        status = "interrupted"
    stops = ", ".join(f"{k}: {v}" for k, v in (row["stops"] or {}).items())
    used = "-" if row["requests_used"] is None else f"{row['requests_used']:,}"
    line = (f"  #{row['run_id']:<4} {row['job']:<7} {fmt_time(row['started_at'])}  "
            f"{fmt_duration(row['started_at'], row['finished_at']):>7}  {status:<11} "
            f"{used:>7}/{row['requests_budget']:,} requests")
    if stops:
        line += f"  [{stops}]"
    if row["error"]:
        line += f"\n         error: {row['error'][:200]}"
    return line


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show pipeline and database status (read-only).")
    parser.add_argument("--runs", type=int, default=8, help="how many recent runs to list (default 8)")
    args = parser.parse_args(argv)
    from db import get_engine, get_url
    try:
        url = get_url()
        engine = get_engine()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            print(f"Database: {url.host}/{url.database}")
            active = run_is_active(conn)
            print("Run in progress: " + ("YES (a fill or update is running now)" if active else "no"))

            try:
                cursors = dict(conn.execute(text("SELECT source, last_activity_epoch FROM etl_state")).all())
            except ProgrammingError:
                print("\nNo pipeline tables yet. Run `fill` first.")
                return 0
            site = next(iter(cursors), "stackoverflow:").split(":")[0]
            print("\nCursors")
            print(f"  update continues from : {fmt_epoch(cursors.get(site + ':questions'))}")
            print(f"  fill continues before : {fmt_epoch(cursors.get(site + ':backfill'))}")

            print("\nRows")
            for table in TABLES:
                n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                print(f"  {table:<20} {n:>12,}")
            span = conn.execute(text(
                "SELECT min(creation_date), max(creation_date), max(last_activity_date) FROM questions")).one()
            if span[0] is not None:
                print(f"  questions created {fmt_time(span[0])} .. {fmt_time(span[1])}; "
                      f"latest activity {fmt_time(span[2])}")
            months = conn.execute(text(
                "SELECT min(month), max(month), count(*) FILTER (WHERE is_partial_month) "
                "FROM tag_monthly_counts")).one()
            if months[0] is not None:
                print(f"  monthly counts cover {months[0]} .. {months[1]} ({months[2]} partial)")

            print(f"\nLast {args.runs} runs")
            try:
                rows = conn.execute(text(
                    "SELECT run_id, job, started_at, finished_at, status, requests_budget, requests_used, "
                    "stops, error FROM etl_runs ORDER BY run_id DESC LIMIT :n"), {"n": args.runs}).mappings().all()
            except ProgrammingError:
                rows = []   # database from before run history existed
            if not rows:
                print("  none recorded yet")
            # Only the newest unfinished run can be the one holding the lock; older ones were cut off.
            live = next((r["run_id"] for r in rows if r["status"] == "running"), None) if active else None
            for row in rows:
                print(describe_run(row, row["run_id"] == live))
    except OperationalError as e:
        print(f"Status failed: could not connect to Postgres ({str(e.orig).strip().splitlines()[0]})",
              file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Status failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
