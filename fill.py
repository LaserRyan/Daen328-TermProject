"""Fill: pull Stack Overflow data we don't have yet. Run it again and again until it says done.

Each run spends at most SE_MAX_REQUESTS API calls on:
  1. monthly question counts for months not stored yet (dashboard Q1, Q2),
  2. questions + answers older than the backfill cursor, newest first, down to SE_BACKFILL_FLOOR.

The cursor lives in the database (etl_state), so the next person's run, with their own API key,
continues exactly where this one stopped. Nothing is ever fetched twice on purpose, and
anything that is is absorbed by the upserts.

    python fill.py
    python fill.py --max-requests 500 --skip-counts
    docker compose run --rm fill
"""
import sys

from etl_common import ensure_seeded, make_parser, run_backfill, run_counts, run_job


def main(argv=None):
    parser = make_parser("Fill the database with Stack Overflow data it doesn't have yet.")
    parser.add_argument("--skip-counts", action="store_true", help="don't fetch missing monthly counts")
    args = parser.parse_args(argv)

    def body(ctx):
        ensure_seeded(ctx)
        if not args.skip_counts:
            run_counts(ctx)
        run_backfill(ctx)

    return run_job("fill", args, body)


if __name__ == "__main__":
    sys.exit(main())
