"""Tests for db.py's choice of database. No connection is made."""
import pytest

import db

DB_VARS = ["DATABASE_URL", "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE",
           "PGCHANNELBINDING", "PGPORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
           "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_SSLMODE"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(db, "load_dotenv", lambda: None)   # never read the developer's real .env
    for name in DB_VARS:
        monkeypatch.delenv(name, raising=False)


def test_direct_host_removes_the_pooler_suffix():
    assert db.direct_host("ep-cool-123-pooler.us-east-2.aws.neon.tech") == "ep-cool-123.us-east-2.aws.neon.tech"
    assert db.direct_host("ep-cool-123.us-east-2.aws.neon.tech") == "ep-cool-123.us-east-2.aws.neon.tech"
    assert db.direct_host(None) is None


def test_database_url_wins_and_is_made_direct(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@ep-a-pooler.r.neon.tech/neondb?sslmode=require")
    monkeypatch.setenv("PGHOST", "ignored")
    url = db.get_url()
    assert url.drivername == "postgresql+psycopg2"
    assert (url.host, url.database, url.query["sslmode"]) == ("ep-a.r.neon.tech", "neondb", "require")


def test_neon_pg_variables_are_accepted_and_the_pooled_host_made_direct(monkeypatch):
    monkeypatch.setenv("PGHOST", "ep-a-pooler.r.neon.tech")
    monkeypatch.setenv("PGUSER", "neon_user")
    monkeypatch.setenv("PGPASSWORD", "pw")
    monkeypatch.setenv("PGDATABASE", "neondb")
    monkeypatch.setenv("PGSSLMODE", "require")
    monkeypatch.setenv("PGCHANNELBINDING", "require")
    monkeypatch.setenv("POSTGRES_USER", "ignored")   # lower priority
    url = db.get_url()
    assert (url.username, url.host, url.database, url.port) == ("neon_user", "ep-a.r.neon.tech", "neondb", 5432)
    assert dict(url.query) == {"sslmode": "require", "channel_binding": "require"}


def test_partial_neon_variables_say_what_is_missing(monkeypatch):
    monkeypatch.setenv("PGHOST", "ep-a.r.neon.tech")
    with pytest.raises(RuntimeError, match="PGUSER, PGPASSWORD, PGDATABASE"):
        db.get_url()


def test_local_database_ignores_neon_ssl_settings(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("POSTGRES_DB", "stackexchange")
    monkeypatch.setenv("PGSSLMODE", "require")   # would break the SSL-less local container
    url = db.get_url()
    assert url.host == "db" and url.query["sslmode"] == "prefer"


def test_nothing_configured_explains_the_options():
    with pytest.raises(RuntimeError, match="DATABASE_URL.*PGHOST.*POSTGRES_USER"):
        db.get_url()
