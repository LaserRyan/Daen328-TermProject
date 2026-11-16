# Running the pipeline with Docker

For everyone on the team (and graders). You only need Docker: no Python, no packages. You can point the
pipeline at either of two databases, set in `.env`:

| Setup | Database | Use it for |
|---|---|---|
| **A. Neon** | our shared hosted PostgreSQL | day-to-day team work: everyone builds the dashboard on the same data |
| **B. Local** | a PostgreSQL container on your own machine | graders, trying it out, or working offline |

The commands are identical for both; only `.env` (and one extra start command for local) differs.

| Service | What it is | Starts when |
|---|---|---|
| `fill` | pulls Stack Overflow data the database doesn't have yet | you run it |
| `update` | upserts questions that changed since the last run | you run it |
| `status` | read-only report of what is in the database and what the last runs did | you run it |
| `db` | the local PostgreSQL for setup B | you start it with `--profile local` |

`fill`, `update` and `status` are one-shot containers: they run, print a summary and exit.
How the jobs work (cursors, idempotency, budget) is in the [README](../README.md#incremental-pulls-and-idempotency).
The dashboard will get its own service later.

## 1. One-time setup (both setups)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it (wait until it says "running").
2. Get **your own** Stack Exchange API key: [stack-exchange-api-key-tutorial.md](stack-exchange-api-key-tutorial.md).
3. In the repo folder, copy the sample settings:

   ```bash
   cp .env.sample .env          # Windows PowerShell: Copy-Item .env.sample .env
   ```
4. Edit `.env` (private and git-ignored; never commit it or paste your key anywhere). Set `SE_API_KEY=` to your
   key, then follow **one** of the two setups below. Everything else can stay as it is; each variable is explained
   in `.env.sample`.

### Setup A: Neon (shared team database)

Put the team's Neon connection details in `.env` in **either** of the two formats Neon's "Connect" dialog offers
(ask Carson for the details):

- **One line:** `DATABASE_URL=postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require`
- **Separate lines** (what you get from Neon's "Parameters only" tab, can be pasted as is):
  `PGHOST=`, `PGDATABASE=`, `PGUSER=`, `PGPASSWORD=`, `PGSSLMODE=`, `PGCHANNELBINDING=`

If both are present, `DATABASE_URL` wins. You don't need the `POSTGRES_*` values. The pipeline creates tables and
takes a database lock, which need Neon's **direct** connection rather than the pooled one. Neon often shows the
pooled host (it contains `-pooler`); the pipeline removes `-pooler` for you, so pasting either is fine.
There is no database container to start. Check the connection:

```bash
docker compose run --rm status
```

The first run builds the image (a couple of minutes), then prints `Database: ep-xxxx.../dbname`.

Neon specifics: the database is shared, so cursors and data are shared too (that's the point: see
[Sharing the daily quota](#3-typical-workflows)). A free Neon database pauses when idle, so the first connection
after a quiet spell can take a few seconds. Neon's plan has a size limit; see "Storage" below.

### Setup B: Local database

In `.env`, leave `DATABASE_URL=` **blank**, remove any Neon `PGHOST` lines (they take priority), and set `POSTGRES_USER=` and `POSTGRES_PASSWORD=` (any values you like;
`POSTGRES_DB` can stay `stackexchange`). Then start the database container once:

```bash
docker compose --profile local up -d --wait db
```

After that the same `fill` / `update` / `status` commands use it. Check:

```bash
docker compose run --rm status
```

which prints `Database: db/stackexchange`. Local specifics:

- The data lives in a Docker volume (`pgdata`) on your machine and survives restarts. It starts **empty**, with its
  own cursors, so your first `fill` starts from scratch. It is completely separate from Neon.
- Stop it with `docker compose --profile local stop db`; start it again with the `up` command above.
- **Wipe it** (data and cursors) with `docker compose --profile local down -v`.
- SQL prompt: `docker compose exec db psql -U <your POSTGRES_USER> -d stackexchange`.
- It listens on port 5432 of your machine. If that is taken, set `POSTGRES_PORT_ON_HOST=5433` in `.env` and
  start it again.

## 2. Commands (both setups)

Run these from the repo folder.

| I want to... | Command |
|---|---|
| Pull data we don't have yet | `docker compose run --rm fill` |
| Pull a small test amount (100 API calls) | `docker compose run --rm fill python fill.py --max-requests 100` |
| Pick up what changed since the last run | `docker compose run --rm update` |
| See what's in the database and what happened | `docker compose run --rm status` |
| Run the tests (no database needed) | `docker compose run --rm fill pytest` |
| Run a long job in the background | `docker compose run -d --rm --name fill_run fill` then `docker logs -f fill_run` |
| Stop a background job | `docker stop fill_run` (safe: see [Interrupted runs](#6-interrupted-runs)) |
| Rebuild after code changes (e.g. after a `git pull`) | `docker compose --profile "*" build` |

Windows tip: put options *after* the service, as in the table (`fill python fill.py --max-requests 100`).
Setting a variable in front of the command (`SE_MAX_REQUESTS=100 docker compose ...`) doesn't work in PowerShell.

## 3. Typical workflows

**Sharing the daily quota (Neon).** Each person has about 10,000 API calls a day on their own key and their own
network (the limit is per key *and* IP, so two people on the same campus Wi-Fi share one). A run spends at most
`SE_MAX_REQUESTS` calls (default 10,000) and then stops cleanly. The cursors live in the database, so the next person
just runs `docker compose run --rm fill` and it continues where the last person's budget ran out. Run `status` first
to see where things stand. (With a local database the same applies, but it's just you.)

**Filling up.** Repeat `fill` (one run per person per day) until the summary says
`reached SE_BACKFILL_FLOOR, nothing left to fill`.

**Staying current.** Run `update` whenever you want fresh data. It only fetches questions that changed since
the last update (and refreshes the current month's counts), so it is cheap. It needs at least one `fill` first.

**One run at a time.** A run holds a database lock. If you see
`another run is in progress against this database`, a teammate (or you, in another window) is running; wait for
them to finish. `status` shows `Run in progress: YES` while a run is active.

**Storage.** A full 10,000-call `fill` adds a lot of rows (very roughly half a million questions plus their
answers). Local disks cope; Neon's plan has a size limit. Don't move `SE_BACKFILL_FLOOR` earlier on Neon without
checking with the team how much room is left.

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
| request budget spent | used all `SE_MAX_REQUESTS` calls | run it again (tomorrow, or a teammate) |
| API quota nearly used up | Stack Exchange says the key is nearly out for the day | wait for the daily reset |
| reached `SE_BACKFILL_FLOOR` | `fill` has everything back to the floor | nothing; use `update` |
| stopped at the page cap | `update` hit `SE_MAX_PAGES` | run `update` again |
| last batch dropped | the budget ran out before a batch's answers were fetched, so it wasn't loaded | nothing: it is fetched again next run |

`retrying ... throttle_violation` lines are normal now and then; the job waits out the API's backoff and
carries on.

## 5. Monitoring

- **`docker compose run --rm status`** shows whether a run is active (yours or a teammate's), both cursors, row
  counts per table, how far back the data reaches, and the last runs (when, how long, requests used, why each
  stopped, any error). A run whose process was killed shows as `interrupted`.
- **`docker logs -f <container>`** follows a background job live.
- **`etl_runs`** is the same run history as a table. Query it in Neon's SQL editor, `psql`, or any SQL client:

  ```sql
  SELECT run_id, job, started_at, status, requests_used, requests_budget, stops, error
  FROM etl_runs ORDER BY run_id DESC LIMIT 10;
  ```
- **`data/raw/<timestamp>/`** (on your machine) keeps the raw API pages of every run, without user data. You can
  replay one into the database without any API calls:
  `docker compose run --rm fill python load.py data/raw/<timestamp>`. These folders are git-ignored and can get
  large (hundreds of MB after a full run); delete old ones freely.

## 6. Interrupted runs

Stopping a job (`docker stop`, Ctrl-C, closing the laptop, losing Wi-Fi) is safe. Every batch is saved together
with its cursor in one transaction, so at most the batch in progress is lost, and the next run redoes it.
Nothing is duplicated, because every write is an upsert. If a run dies without cleaning up, the database lock
is released automatically when its connection drops.

## 7. Troubleshooting

| You see | Setup | Cause and fix |
|---|---|---|
| `failed to connect to the docker API` / `cannot find the file specified` | both | Docker Desktop isn't running. Start it and wait for "running". |
| `no database configured in .env` | A | Neither `DATABASE_URL` nor Neon's `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` lines are in `.env`. Add them. |
| `could not connect to Postgres (...)` | A | Check the Neon details in `.env` (password correct, no stray spaces). If Neon was idle, retry after a few seconds. |
| `could not translate host name "db"` or `connection refused` | B | The local database isn't running. Start it: `docker compose --profile local up -d --wait db`. |
| `no database configured in .env` | B | Set `POSTGRES_USER` and `POSTGRES_PASSWORD` in `.env` (and remove any `DATABASE_URL` / `PGHOST` lines, which take priority). |
| `password authentication failed` | B | The volume was created with different credentials. Use the original ones, or wipe it: `docker compose --profile local down -v`. |
| `port is already allocated` on 5432 | B | Set `POSTGRES_PORT_ON_HOST=5433` in `.env` and start it again. |
| `SE_API_KEY is blank` warning | both | Anonymous calls are capped at 300 a day. Add your key to `.env`. |
| `API error ... No site found` | both | `SE_SITE` in `.env` is wrong (it should be `stackoverflow`). |
| `another run is in progress` | both | See "One run at a time" above. |
| `nothing to update yet: run fill first` | both | `update` needs `fill` to have run once against this database. |
| Edited code but the container behaves the same | both | Rebuild: `docker compose --profile "*" build` (do this after every `git pull`). |
| `docker logs fill_run` says `No such container` right after starting a background job | both | The job crashed immediately and `--rm` deleted the container with its logs. Run it in the foreground to see the error: `docker compose run --rm fill`. |

## 8. How it's put together

- One `Dockerfile` (Python 3.12, pinned `requirements.txt`) builds the image the three jobs share. The
  `docker-compose.yml` picks the job with `command:`.
- `fill`, `update` and `status` sit behind compose *profiles*, so a plain `docker compose up` never starts a job.
  The local `db` sits behind the `local` profile, so Neon users never start it.
- `.env` is passed in at run time and is never copied into the image (`.dockerignore`). `DATABASE_URL`, when set,
  wins over the `POSTGRES_*` values. Inside compose the local database's host is always `db`.
- `./data` is mounted into the containers, so raw API pages survive after a container exits.
