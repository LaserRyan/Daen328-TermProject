# Extraction layer (owner: Carson)

Source: Stack Exchange API v2.3 (`api.stackexchange.com`), default site `stackoverflow`.
Code: [`extract.py`](../extract.py), [`db.py`](../db.py). Tests: [`tests/test_extract.py`](../tests/test_extract.py).

```
Stack Exchange API
   │  custom filter (no user fields) + paging, retries, backoff
   ▼
extract.py ──► data/raw/<run>/*.json   (scrubbed; gitignored; replayable)
   │
   ▼  ExtractResult
transform.py (Jeremy) ──► load.py (David) ──► Postgres ──► streamlit_app.py
                                   └─ save_watermark(...) only after commit
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
2. Copy `.env.sample` to `.env` and fill it in. To get a key, follow [the Stack Exchange key tutorial](../Stack%20Exchange%20API%20Instructions/stack-exchange-api-key-tutorial.md). The variable is **`SE_API_KEY`**, which the tutorial also uses.
3. Run:

```bash
python extract.py --no-db --since 2026-11-10 --skip-counts   # quick check, no database
python extract.py --from-raw data/raw/<run_stamp>             # replay a saved run, zero API calls
```

| Variable | Default | Meaning |
|---|---|---|
| `SE_API_KEY` | blank | Free key. Blank means anonymous (300 requests/day), which is enough for the quick check but **not** a full first run. |
| `SE_SITE` | `stackoverflow` | Which Stack Exchange site. |
| `SE_START_DATE` | blank (60 days ago) | First-run window for row-level questions. |
| `SE_MAX_PAGES` | `300` | Cap on question pages per run (100 per page). |
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

- **What marks a record as new or changed:** the question's `last_activity_date`. Requests use `sort=activity&order=asc&min=<epoch>`, which returns every question with activity at or after that time. That covers new questions and questions that changed (an edit, a new answer, a close).
- **Position:** the watermark is the maximum `last_activity_date` actually received, stored in Postgres in `etl_state`:

  ```sql
  etl_state(source VARCHAR(100) PRIMARY KEY, last_activity_epoch BIGINT NOT NULL, last_run_at TIMESTAMPTZ NOT NULL)
  ```
  The `source` value is `"<site>:questions"`.
- **First run** (no watermark): `min = SE_START_DATE`. Monthly counts run from `SE_HISTORY_START`.
- **Later runs:** `min = watermark − 300s`. The 5-minute overlap guards against clock skew and ties, so the same question can arrive twice. The load upsert removes the repeat. Only the previous and current month's counts are refreshed.
- **When the watermark moves:** `save_watermark(engine, site, result.max_activity_epoch)` is called **only after load has committed**, so a failed load is retried next run. Because order is ascending, a run stopped by the page cap or low quota still leaves a correct position.
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
etl_state(source PK, ...)      -- see section 4; save_watermark creates it if missing
```

- No `users` table.
- Upsert everything with `INSERT ... ON CONFLICT ... DO UPDATE`. The answers stream can include answers whose question was already loaded; that's fine.
- The `etl_state` table is created by `save_watermark`. If you'd rather create it in your schema script, keep the same column names.
- `db.get_engine()` builds the SQLAlchemy engine from the `POSTGRES_*` variables.

### Jonathan (Docker)

- The `etl` service should wait for the database (`depends_on` with a healthcheck, `condition: service_healthy`). `POSTGRES_HOST` is the compose service name (`db` by default).
- Pass `.env` through to `etl` and the dashboard.
- Expect the first full run to take a while. With a key it's about 1,000 requests, paced at roughly a quarter second each plus the network time. Don't set a short healthcheck or timeout on `etl`.
- `data/` (raw files) is gitignored. Mounting it as a volume is optional.

### Everyone (dashboard)

The table in section 3 shows which stream answers which question. Q1 and Q2 come from `tag_monthly_counts`, not from counting `questions` rows, since `questions` only covers the recent window.

## 6. Failure behavior

Every failure becomes one line on stderr and exit code 1, with no traceback:

| Situation | What happens |
|---|---|
| Bad site, bad parameter, bad key | Fails immediately with the API's own message, e.g. `No site found for name 'notasite'`. |
| Timeout, 5xx, throttle (`error_id` 502) | Retries 3 times with growing waits, then `gave up after 3 attempts`. |
| API `backoff` field | The next request waits that long. |
| `quota_remaining < 10` | The stream stops early with a message and keeps what it already has. |
| Postgres unreachable | `could not connect to Postgres (...)`. |
| Blank `SE_API_KEY` | Warning only, so the quick check still works. |

## 7. Summary fields extract provides

`summary_lines(result)` returns the rows received per stream, whether the run was full or incremental (and since when), the pages fetched and the quota remaining. Transform and load add cleaning and insert/update counts to the final ETL summary.

## 8. Open questions

- Final `SE_TRACKED_TAGS` list.
- Length of the row-level window (`SE_START_DATE`) for Q3–Q5.
- Duplicate-closed questions: keep or exclude?
- Will we mount `data/raw` as a volume?
