# Running the pipeline with Docker

For everyone on the team. You only need Docker: no Python, no packages. (The dashboard will get its own
service later; the files here only cover the database and the two pipeline jobs.)

| Service | What it is | Starts when |
|---|---|---|
| `db` | local PostgreSQL 16 with a saved volume | automatically, whenever a job needs it |
| `fill` | pulls Stack Overflow data the database doesn't have yet | you run it |
| `update` | upserts questions that changed since the last run | you run it |
| `status` | read-only report of what is in the database and what the last runs did | you run it |

`fill`, `update` and `status` are one-shot containers: they run, print a summary and exit.
How the jobs work (cursors, idempotency, budget) is in the [README](../README.md#incremental-pulls-and-idempotency).

## 1. One-time setup

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it (wait until it says "running").
2. Get your own Stack Exchange API key: [stack-exchange-api-key-tutorial.md](stack-exchange-api-key-tutorial.md).
3. In the repo folder, copy the sample settings and fill them in:

   ```bash
   cp .env.sample .env          # Windows PowerShell: Copy-Item .env.sample .env
   ```

   `.env` is private and git-ignored. Never commit it or paste your key anywhere. Pick **one** of these two setups:

   **A. Local database** (graders, or trying it out). Set `SE_API_KEY`, then choose any `POSTGRES_USER` and
   `POSTGRES_PASSWORD`. Leave `DATABASE_URL` blank. The data lives in a Docker volume on your machine.

   **B. The team's shared Neon database.** Set `SE_API_KEY` and paste the team's **direct** connection string
   into `DATABASE_URL` (the host must *not* contain `-pooler`; ask Carson for it). You don't need the
   `POSTGRES_*` values. Add `--no-deps` to every command below so the local `db` isn't started
   (e.g. `docker compose run --rm --no-deps fill`).

   Other `.env` variables you may care about: `SE_MAX_REQUESTS` (API calls per run, default 10,000) and
   `SE_BACKFILL_FLOOR` (how far back `fill` goes). All of them are explained in `.env.sample`.

## 2. Commands

Run these from the repo folder. The first run builds the image, which takes a couple of minutes.

| I want to... | Command |
|---|---|
| Pull data we don't have yet | `docker compose run --rm fill` |
| Pull a small test amount (100 API calls) | `docker compose run --rm fill python fill.py --max-requests 100` |
| Pick up what changed since the last run | `docker compose run --rm update` |
| See what's in the database and what happened | `docker compose run --rm status` |
| Run the tests | `docker compose run --rm fill pytest` |
| Run a long job in the background | `docker compose run -d --rm --name fill_run fill` then `docker logs -f fill_run` |
| Open a SQL prompt (local database) | `docker compose exec db psql -U <your POSTGRES_USER> -d stackexchange` |
| Stop a background job | `docker stop fill_run` (it is safe: see "Interrupted runs" below) |
| Start over with an empty local database | `docker compose down -v` (deletes the local data; also deletes the saved cursors) |

Windows tip: put the options *after* the service as in the table (`fill python fill.py --max-requests 100`).
Setting a variable in front of the command (`SE_MAX_REQUESTS=100 docker compose ...`) doesn't work in PowerShell.

## 3. Typical workflows

**First time, local database.** Run `docker compose run --rm fill` and repeat it until the summary says
`reached SE_BACKFILL_FLOOR, nothing left to fill`. Each run spends up to `SE_MAX_REQUESTS` API calls and picks
up exactly where the last one stopped. Then use `update` to keep current.

**Sharing the daily quota on Neon.** Each person has about 10,000 API calls a day on their own key and
their own network (the limit is per key *and* IP, so two people on the same campus Wi-Fi share one). The
cursors live in the database, so the next person simply runs `docker compose run --rm --no-deps fill` and it
continues where the last person's budget ran out. Run `status` first if you want to see where things stand.

**One run at a time.** A run holds a database lock. If you see
`another run is in progress against this database`, someone (maybe you, in another window) is running; wait
for them to finish. `status` shows `Run in progress: YES` while a run is active.

## 4. Reading the output

While a job runs it prints one line per batch, so you can see it working:

```
[06:40:28] fill run #7 started: budget 10,000 requests, site stackoverflow
[06:40:38] backfill: loaded 1,000 questions + 1,299 answers; cursor now 2026-04-20 (requests 26/10,000, API quota left 8906)
```

At the end it prints a summary: requests used, why each stream stopped, rows inserted/updated, and where the
cursors are. The stop reasons:

| Reason | Meaning | What to do |
|---|---|---|
| finished, nothing more to fetch | caught up | nothing |
| request budget spent | used all `SE_MAX_REQUESTS` calls | run it again (tomorrow, or someone else) |
| API quota nearly used up | Stack Exchange says the key is nearly out for the day | wait for the daily reset |
| reached `SE_BACKFILL_FLOOR` | `fill` has everything back to the floor | nothing; use `update` |
| stopped at the page cap | `update` hit `SE_MAX_PAGES` | run `update` again |
| last batch dropped | the budget ran out before a batch's answers were fetched, so it wasn't loaded | nothing: it is fetched again next run |

`Stack Exchange throttle` retries (`retrying ... throttle_violation`) are normal now and then; the job waits
out the API's backoff and carries on.

## 5. Monitoring

- **`docker compose run --rm status`** shows whether a run is active, both cursors, row counts per table, how
  far back the data reaches, and the last runs (when, how long, requests used, why each stopped, any error).
  A run whose process was killed shows as `interrupted`.
- **`docker logs -f <container>`** follows a background job live.
- **`etl_runs`** is the same run history as a table, so you can query it:

  ```sql
  SELECT run_id, job, started_at, status, requests_used, requests_budget, stops, error
  FROM etl_runs ORDER BY run_id DESC LIMIT 10;
  ```
- **`data/raw/<timestamp>/`** keeps the raw API pages of every run (without user data). You can replay one into
  the database without any API calls: `docker compose run --rm fill python load.py data/raw/<timestamp>`.
  These folders are git-ignored and can get large (hundreds of MB after a full run); delete old ones freely.

## 6. Interrupted runs

Stopping a job (`docker stop`, Ctrl-C, closing the laptop) is safe. Every batch is saved together with its
cursor in one transaction, so at most the batch in progress is lost, and the next run redoes it. Nothing is
duplicated, because every write is an upsert.

## 7. Troubleshooting

| You see | Cause and fix |
|---|---|
| `failed to connect to the docker API` / `cannot find the file specified` | Docker Desktop isn't running. Start it and wait for "running". |
| `missing in .env: POSTGRES_USER...` | Setup A needs `POSTGRES_USER` and `POSTGRES_PASSWORD` in `.env` (or set `DATABASE_URL`). |
| `could not connect to Postgres (...)` | Local: run `docker compose up -d db` and try again. Neon: check `DATABASE_URL` is the direct string and you used `--no-deps`. |
| `SE_API_KEY is blank` warning | Anonymous calls are capped at 300 a day. Add your key to `.env`. |
| `API error ... No site found` | `SE_SITE` in `.env` is wrong (it should be `stackoverflow`). |
| `another run is in progress` | See "One run at a time" above. |
| `nothing to update yet: run fill first` | `update` needs `fill` to have run once. |
| `port is already allocated` on 5432 | Something else uses 5432. Set `POSTGRES_PORT_ON_HOST=5433` in `.env`. |
| Edited code but the container behaves the same | Rebuild: `docker compose --profile "*" build`. |
| Old `PG*` Neon variables in `.env` | Harmless. The pipeline reads `DATABASE_URL`, not `PGHOST` and friends. |

## 8. How it's put together

- One `Dockerfile` (Python 3.12, pinned `requirements.txt`) builds the image all three jobs share. The
  `docker-compose.yml` picks the job with `command:`.
- `fill`, `update` and `status` sit behind compose *profiles*, so a plain `docker compose up` starts only the
  database and the jobs never run at the same time by accident.
- Inside compose the database host is always `db`, whatever `.env` says. `DATABASE_URL`, when set, wins.
- `.env` is passed in at run time and is never copied into the image (`.dockerignore`).
- `./data` is mounted into the containers, so raw API pages survive after a container exits.
- The database healthcheck makes jobs wait until Postgres is ready.
