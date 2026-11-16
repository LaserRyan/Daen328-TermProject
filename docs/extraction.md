# Extraction layer (owner: Carson)

Source: Stack Exchange API v2.3 (`api.stackexchange.com`), default site `stackoverflow`.
Code: [`extract.py`](../extract.py) (API client only), [`etl_common.py`](../etl_common.py), [`fill.py`](../fill.py), [`update.py`](../update.py), [`db.py`](../db.py). Tests: [`tests/test_extract.py`](../tests/test_extract.py), [`tests/test_etl.py`](../tests/test_etl.py).

```
Stack Exchange API
   │  custom filter (no user fields) + paging, retries, backoff
   ▼
extract.py ──► data/raw/<run>/*.json   (scrubbed; gitignored; replayable)
   │
   ▼  ExtractResult
transform.py (Jeremy) ──► load.py (David) ──► Postgres ──► streamlit_app.py
                                   └─ cursor (etl_state) written in the same transaction as the rows

fill.py    = counts we don't have + questions older than the backfill cursor
update.py  = questions with activity since the watermark (+ current month's counts)
```

## 1. Privacy decision (our Phase 1 risk)

Display names can be real names and profile images can be real faces, so **we never collect user data**.

- The API filter is a whitelist, so `owner`, `last_editor`, post bodies and comments are never returned.
- `drop_user_fields()` removes `owner`, `last_editor`, `user_id`, `account_id`, `display_name`, `profile_image` and `reply_to_user` again before anything is written to disk or handed on. A test covers this.
- There is no `users` table. Dashboard question 4 ("attention") uses counts only: `score`, `comment_count`, `view_count`.
- Please don't add user fields back, including for convenience in a join. If a question seems to need them, raise it with the team first.
- Each question keeps its `link`, which provides the CC BY-SA attribution. The dashboard should link back.

## 2. Setup

1. `pip install -r requirements.txt`
2. Copy `.env.sample` to `.env` and fill it in. To get a key, follow [the Stack Exchange key tutorial](stack-exchange-api-key-tutorial.md). The variable is **`SE_API_KEY`**, which the tutorial also uses.
3. Run:

```bash
python extract.py --since 2026-11-10                  # quick check: one page, no database
python extract.py --from-raw data/raw/<run_stamp>     # replay a saved run, zero API calls
python fill.py                                        # the real pipeline: see the README
python update.py
```

| Variable | Default | Meaning |
|---|---|---|
| `SE_API_KEY` | blank | Free key. Blank means anonymous (300 requests/day), which is enough for the quick check but **not** for `fill`. |
| `SE_SITE` | `stackoverflow` | Which Stack Exchange site. |
| `SE_MAX_REQUESTS` | `10000` | API calls one run may spend (the daily quota with a key). |
| `SE_BACKFILL_FLOOR` | `2025-01-01` | `fill` stops walking back at this creation date. |
| `SE_BATCH_PAGES` | `10` | Question pages per committed batch. |
| `SE_MAX_PAGES` | `300` | Cap on question pages per `update` run (100 per page). |
| `SE_HISTORY_START` | `2020-01-01` | Monthly counts start here. |
| `SE_TRACKED_TAGS` | 8 big tags, `;` separated | Tags that get monthly counts. |
| `SE_REQUEST_INTERVAL` | `0.25` | Seconds between calls. |

The quota is per key **and** IP. Campus networks often share one public IP, so run the first full pull from one person's machine, or spread the work across networks.

## 3. What extract pulls, and which dashboard question it serves

| Dashboard question | Stream | Endpoint |
|---|---|---|
| Q3 tags that co-occur, Q4 attention by tag | **questions** (row level) | `/questions` |
| Q5 time to first / accepted answer | **questions + answers** (row level) | `/questions`, `/questions/{ids}/answers` |
| Q1 tag growth, Q2 site activity over time | **monthly counts** | `/questions?filter=total`, one request per month and scope |

Pulling every question back to 2020 isn't possible within quota. Instead, Q1 and Q2 use the API's `filter=total`, which returns a single count per request: site-wide, plus one for each tracked tag. Row-level questions cover a recent window only.

## 4. Incremental pulls (for the README section)

The final version of this section is in the [README](../README.md#incremental-pulls-and-idempotency). In short:

- **New or changed records:** `update` walks forward by `last_activity_date` from the watermark (`etl_state` row `<site>:questions`), with `min = watermark - 300s`. The 5-minute overlap guards against clock skew and ties, so the same question can arrive twice; the load upsert absorbs it.
- **Data we don't have:** `fill` walks backward by `creation_date` from the cursor (`etl_state` row `<site>:backfill`) down to `SE_BACKFILL_FLOOR`.
- **First run:** the first `fill` records "now" as the starting point of both cursors, so the two jobs meet with no gap.
- **When a cursor moves:** inside the transaction that loads the batch, so a failed load leaves it where it was. A run stopped by the page cap, budget or quota still leaves a correct position, because both walks are ordered.
- **Budget:** each run spends at most `SE_MAX_REQUESTS` calls and stops when the API's `quota_remaining` is below 10. The next run (anyone's) continues from the stored cursors.
- **Monthly counts:** `fill` requests only months not stored yet; `update` refreshes the previous and current month.
- **Caveat:** an incremental pull can return questions that are years old, because old questions get new activity. That's expected, and `creation_date` is the field for "when was it asked".

## 5. Handoffs

### Jeremy (transform): input is an `ExtractResult`

`questions`, `answers` and `monthly_counts` hold raw API dicts minus user fields. Extract does **no** cleaning, so all of it is yours.

Question fields: `question_id, title, tags[], score, view_count, answer_count, comment_count, is_answered, accepted_answer_id?, creation_date, last_activity_date, last_edit_date?, closed_date?, closed_reason?, link`.
Answer fields: `answer_id, question_id, score, is_accepted, creation_date, last_activity_date, last_edit_date?`.
Monthly count rows: `{scope: "site" | "<tag>", month: "YYYY-MM", question_count, is_partial_month}`.

Things to expect, from real responses:
- **Dates are Unix seconds** (`creation_date` and the rest). They need converting to datetimes, as in the transform notes.
- `?` fields are **missing** rather than null when they don't apply (`closed_*` only on closed questions, `accepted_answer_id` only when accepted, `last_edit_date` only if edited).
- `title` can contain HTML entities (`&#39;`, `&amp;`).
- `closed_reason: "Duplicate"` is the open discussion from the Phase 1 notes: keep these rows or exclude them from the attention and answer-time questions? It needs a team decision.
- The current month in `monthly_counts` is partial (`is_partial_month`). Don't chart it as a drop.
- Duplicates can arrive across overlap windows, so dedupe on `question_id` and `answer_id`.
- `tags` is a list. It needs exploding into a `question_tags` table for Q3.

### David (load): draft schema

```
questions(question_id PK, title, score, view_count, answer_count, comment_count, is_answered,
          accepted_answer_id, creation_date, last_activity_date, last_edit_date, closed_date, closed_reason, link)
answers(answer_id PK, question_id FK -> questions, score, is_accepted, creation_date, ...)
tags(tag_name PK)
question_tags(question_id FK, tag_name FK, PRIMARY KEY (question_id, tag_name))
tag_monthly_counts(scope, month, PRIMARY KEY (scope, month), question_count)
etl_state(source PK, last_activity_epoch, last_run_at)   -- the two cursors, see section 4; defined in load.py
```

- No `users` table.
- Upsert everything with `INSERT ... ON CONFLICT ... DO UPDATE`. The answers stream can include answers whose question was already loaded; that's fine.
- `etl_state` is defined in `load.py` next to the other tables and created by `create_schema`.
- `db.get_engine()` builds the SQLAlchemy engine from `DATABASE_URL` (Neon) or the `POSTGRES_*` variables.

### Jonathan (Docker)

One image, three compose services: `db`, `fill` and `update` (see `docker-compose.yml`). The jobs wait for the database healthcheck, get `.env` passed in, and mount `./data` for the raw files. They sit behind profiles, so run them with `docker compose run --rm fill` / `update`. A full `fill` takes 1-2 hours, so there are no short timeouts.

### Everyone (dashboard)

The table in section 3 shows which stream answers which question. Q1 and Q2 come from `tag_monthly_counts`, not from counting `questions` rows, since `questions` only covers the recent window.

## 6. Failure behavior

Every failure becomes one line on stderr and exit code 1, with no traceback:

| Situation | What happens |
|---|---|
| Bad site, bad parameter, bad key | Fails immediately with the API's own message, e.g. `No site found for name 'notasite'`. |
| Timeout, 5xx, non-JSON 5xx | Retries 3 times with growing waits, then `gave up after 3 attempts`. |
| Throttle (`error_id` 502, "Violation of backoff parameter") | Waits out the API's backoff (at least 10s, longer each attempt) and retries up to 6 times, then gives up. |
| API `backoff` field, on any response | The next request waits that long plus 1 second. |
| `quota_remaining < 10`, or `SE_MAX_REQUESTS` spent | The stream stops early, the finished batches stay loaded, and the summary says why. A batch whose answers weren't all fetched is dropped and retried next run. |
| Postgres unreachable | `could not connect to Postgres (...)`. |
| Another run holds the lock | `another run is in progress against this database`. |
| `update` before any `fill` | `nothing to update yet: run fill first`. |
| Blank `SE_API_KEY` | Warning only, so the quick check still works. |

## 7. Run summary

While running, each job prints one timestamped line per loaded batch and records itself in `etl_runs`; `python status.py` (or `docker compose run --rm status`) reports the database and the recent runs. See [docker.md](docker.md#5-monitoring). At the end each job prints requests used of the budget, the API quota remaining, why each stream stopped, the transform lines, the load lines (inserted / updated / skipped as older than stored) and where the cursors are now.

## 8. Open questions

- Final `SE_TRACKED_TAGS` list.
- `SE_BACKFILL_FLOOR` for Q3-Q5, limited by how much data our Neon plan can hold.
- Duplicate-closed questions: keep or exclude?
- ~~Will we mount `data/raw` as a volume?~~ Yes, `./data` is mounted in compose.
