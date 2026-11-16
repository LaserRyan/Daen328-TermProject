"""Update: find questions that changed since the last run and upsert them.

Walks forward by last_activity_date from the watermark stored in etl_state: new questions,
plus old ones with a new answer, edit, vote change or close. Also refreshes the previous and
current month's counts. Needs `fill` to have run once (that sets the starting point).

    python update.py
    python update.py --max-requests 300
    docker compose run --rm update
"""
import sys

from etl_common import make_parser, run_counts, run_forward, run_job


def main(argv=None):
    args = make_parser("Upsert Stack Overflow questions that changed since the last run.").parse_args(argv)

    def body(ctx):
        run_forward(ctx)
        run_counts(ctx, refresh_partial=True, recent_only=True)

    return run_job("update", args, body)


if __name__ == "__main__":
    sys.exit(main())
