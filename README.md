# DAEN 328 Term Project: Stack Overflow Activity Pipeline

A dockerized ETL pipeline that pulls Stack Overflow questions and answers from the
[Stack Exchange API v2.3](https://api.stackexchange.com/docs), cleans them with our own
transform functions, loads them into PostgreSQL, and shows them in a Streamlit dashboard.

No user or account data is collected (see [docs/extraction.md](docs/extraction.md#1-privacy-decision-our-phase-1-risk)).

## Dashboard questions

1. Which tags are growing or shrinking in question volume month over month?
2. How has overall site activity changed over time?
3. Which technologies (tags) tend to appear together?
4. Which tags receive the most attention (score, comments, views)?
5. How long does it take to get a first answer or an accepted answer, by tag?

## Repository layout

```
extract.py          pulls questions, answers, monthly counts from the API   (Carson)
transform.py        cleaning functions                                        (Jeremy)
load.py             schema + upserts into PostgreSQL                          (David)
streamlit_app.py    dashboard                                                 (everyone)
db.py               shared SQLAlchemy engine from .env
tests/              pytest tests
docs/               extraction and transform notes, API key tutorial
Dockerfile, docker-compose.yml                                                (Jonathan)
.env.sample         every environment variable, no secrets
```

## Run it

1. Get a free API key: [docs/stack-exchange-api-key-tutorial.md](docs/stack-exchange-api-key-tutorial.md).
2. `cp .env.sample .env` and fill in `SE_API_KEY` and the `POSTGRES_*` values.
3. `docker-compose up --build`
4. Open the dashboard at http://localhost:8501.

Run the tests with `docker-compose run --rm etl pytest`.

## Layer notes

- [Extraction](docs/extraction.md): sources, privacy, incremental pulls
- [Transform](docs/transform.md): cleaning decisions and next steps

## Schema and ERD

_TODO (David): table descriptions and ERD image._

## Incremental pulls

See [docs/extraction.md, section 4](docs/extraction.md#4-incremental-pulls-for-the-readme-section).
_TODO: copy the final version here once the pipeline is done._

## Dashboard screenshot

_TODO_

## Team

| Member | Contribution |
|---|---|
| Carson Phillips (leader) | Extract |
| Jeremy Suen | Transform |
| David Garudo | Load |
| Jonathan Silberstein | Docker |
| Everyone | Dashboard |
