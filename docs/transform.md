# Transform layer (owner: Jeremy)

Code: [`transform.py`](../transform.py). Tests: [`tests/test_transform.py`](../tests/test_transform.py).

```
extract.py ──ExtractResult──► transform.py ──TransformResult──► load.py ──► Postgres
                               (clean, dedupe, split tags)       (David)
```

## 1. Input and output

**Input:** the `ExtractResult` from `extract.extract()` or `extract.load_raw()`. It holds raw API dicts with the user fields already removed (see [extraction.md](extraction.md#5-handoffs)).

**Output:** a `TransformResult` with one pandas DataFrame per table, plus `stats` for the run summary.

| Table | Primary key | Columns |
|---|---|---|
| `questions` | `question_id` | title, score, view_count, answer_count, comment_count, is_answered, accepted_answer_id, creation_date, last_activity_date, last_edit_date, closed_date, closed_reason, **is_closed**, **is_duplicate_closure**, link |
| `answers` | `answer_id` | question_id (FK), score, is_accepted, creation_date, last_activity_date, last_edit_date |
| `tags` | `tag_name` | none besides the key |
| `question_tags` | (`question_id`, `tag_name`) | both are FKs |
| `tag_monthly_counts` | (`scope`, `month`) | question_count, is_partial_month |

Types: IDs and counts are nullable `Int64`, flags are `boolean`, dates are UTC timezone-aware timestamps (use `TIMESTAMPTZ`), and `month` is a `date` for the first of the month.

## 2. Cleaning functions

Each function does one job and has at least one test.

| Function | Problem it fixes |
|---|---|
| `to_frame` | Optional API fields are missing (not null), so a column can disappear from a batch. This adds every expected column. |
| `drop_missing_keys` | Rows without a primary key can't be upserted or joined, so they're dropped. This is the only automatic row drop. |
| `convert_epoch_columns` | Converts Unix seconds to UTC datetimes. Missing values stay `NaT`. |
| `decode_html_entities` | Decodes `&#39;` and `&amp;` in titles. |
| `strip_whitespace` | Removes extra spaces in titles. |
| `normalize_tags` | Lowercases and trims tags, drops blanks, and removes repeats within a question. |
| `remove_duplicates` | Keeps the newest copy (by `last_activity_date`) of each question or answer. |
| `fill_missing` | Applies the per-column fill policy (section 3) and reports how many values it filled. |
| `cast_types` | Sets nullable integer and boolean types so NULLs reach Postgres intact. |
| `standardize_closed_reason` | `" duplicate "` and `"DUPLICATE"` both become `"Duplicate"`. |
| `add_closed_flags` | Adds `is_closed` and `is_duplicate_closure`. |
| `explode_tags` | Builds the `question_tags` bridge table with one row per question–tag pair. |
| `build_tags_table` | Builds the distinct `tags` table. |
| `parse_month` | Turns `"2026-03"` into `2026-03-01`. Bad values become missing and the row is dropped. |
| `normalize_scope` | Lowercases scopes so they match `tags.tag_name`. |
| `drop_missing_counts` | Drops monthly rows whose count request failed (section 3). |

`transform_questions`, `transform_answers` and `transform_monthly_counts` call these in order, and `transform()` runs all three.

## 3. Decisions made

### Missing values, decided column by column

| Column | Decision | Reason |
|---|---|---|
| `question_id`, `answer_id`, answer `question_id` | **Drop the row** | Without a key the row can't be stored or linked. |
| `score`, `view_count`, `answer_count`, `comment_count` | Fill with **0** | 0 is the neutral value, and the API reports these as 0 when nothing has happened. |
| `is_answered`, `is_accepted`, `is_partial_month` | Fill with **False** | Missing means the event hasn't happened. |
| `title` | Fill with `"(untitled)"` | Keeps the row. This shouldn't happen in practice. |
| `link` | Fill with `""` | Keeps the row. |
| `accepted_answer_id` | **Leave NULL** | NULL means no accepted answer yet, and Q5 depends on that. |
| `last_edit_date` | **Leave NULL** | Never edited. |
| `closed_date`, `closed_reason` | **Leave NULL** | Still open. |
| monthly `question_count` | **Drop the row** | It's only missing when a request failed or quota ran out. A guessed 0 would draw a fake dip on the Q1/Q2 charts, and the next run fetches it again. |

### Duplicates

Extract deliberately overlaps incremental runs by 5 minutes, so the same question can arrive twice. Transform keeps the copy with the latest `last_activity_date`. Load's `ON CONFLICT ... DO UPDATE` still guards against repeats across runs.

### Tags (3NF)

`questions` doesn't store a tag list. Tags live in `tags` and `question_tags`. This replaces the `varchar[]` columns in the Phase 1 ERD.

### Questions closed as duplicates

This is still a team decision. For now the rows are **kept and flagged** with `is_duplicate_closure`. That keeps the site-wide counts honest, and the dashboard can exclude them from Q4 (attention) and Q5 (answer time) with `WHERE NOT is_duplicate_closure`. If the team decides to drop them, it's a one-line change in `transform_questions`.

### Privacy

Transform never adds user fields. Extract already strips them, and `to_frame` keeps only the listed columns, so anything unexpected is dropped too.

## 4. Run summary

`summary_lines(result.stats)` returns lines like:

```
after cleaning: 4,912 questions (88 duplicates removed, 0 without an id dropped), 9,310 answers (12 duplicates removed), 616 monthly counts (0 without a count dropped)
missing values filled: questions: none needed; answers: none needed
missing values kept as NULL (they mean something): creation_date 0, last_activity_date 0, last_edit_date 3,120, closed_date 4,701
tags: 6,054 distinct, 14,233 question-tag pairs
```

These sit between extract's "received from API" lines and load's inserted/updated counts.

## 5. Running it

```bash
python transform.py data/raw/<run_stamp>        # clean a saved extract run, no API or DB
pytest tests/test_transform.py                  # locally
docker compose run --rm fill pytest              # how the TA runs it
```

## 6. Moving forward

### Phase A: working pipeline

- [x] **Run scripts.** `fill.py` and `update.py` (shared logic in `etl_common.py`) run extract, transform, load in batches and write the cursor in the load transaction.
- [x] **Load (David).** See the README "Load layer".
- [x] **Docker.** `Dockerfile` (Python 3.12, pinned requirements) and `docker-compose.yml` with `db`, `fill` and `update`. Tests run with `docker compose run --rm fill pytest`.
- [ ] **Dashboard (everyone).** Q1/Q2 read from `tag_monthly_counts`, and Q3-Q5 read from `questions` + `question_tags` + `answers`. Remember the `is_duplicate_closure` filter and the partial current month.
- [x] **README.** Incremental pulls, idempotency and the team workflow are in the main README. Dashboard screenshot still to do.

### Phase B: pulling more data with all four of us

Done by the same two jobs: each person runs `fill` with their own key and network, and the backfill cursor in the shared database means the next run continues where the last one stopped. Transform needs no changes, because dedupe and guarded upserts make any overlap harmless. Budget roughly 2 requests per 100 answered questions for answers on top of the question pages.

### Open team decisions

1. Keep or exclude questions closed as duplicates? (Currently kept and flagged.)
2. The final `SE_TRACKED_TAGS` list. Monthly counts only exist for these tags.
3. The row-level window (`SE_BACKFILL_FLOOR`) for Q3–Q5, limited by how much data our Neon plan can hold.

### Possible improvements if there's time

- Precompute `hours_to_accepted_answer` per question for Q5 (from the accepted answer's `creation_date` minus the question's `creation_date`), or compute it in SQL in the dashboard.
- Add tag synonyms (e.g. `reactjs` and `react`) if the charts show split tags.
