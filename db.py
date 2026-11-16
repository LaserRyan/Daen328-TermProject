"""Shared Postgres connection, built from .env. First match wins:

1. DATABASE_URL                      a Neon connection string (postgresql://user:pw@host/db?...)
2. PGHOST, PGUSER, PGPASSWORD,       the separate variables Neon's "connection details" page gives
   PGDATABASE (+ PGSSLMODE,          you (it offers both formats; either works here)
   PGCHANNELBINDING, PGPORT)
3. POSTGRES_USER, POSTGRES_PASSWORD, the optional local Postgres container in docker-compose
   POSTGRES_DB, POSTGRES_HOST, POSTGRES_PORT

Neon: the pipeline takes a database-wide lock and creates tables, which need the DIRECT connection,
not the pooled one. Neon's pooled host is the direct host with "-pooler" added to the name, so
direct_host() removes it for you.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

DRIVER = "postgresql+psycopg2"


def direct_host(host):
    """Neon's pooled host (ep-xxx-pooler.region...) -> its direct host (ep-xxx.region...)."""
    return host.replace("-pooler", "", 1) if host and "-pooler" in host else host


def get_url():
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        # Neon hands out postgresql://...; make the psycopg2 driver explicit for SQLAlchemy.
        url = make_url(database_url.replace("postgres://", "postgresql://", 1))
        return url.set(drivername=DRIVER, host=direct_host(url.host))

    if os.getenv("PGHOST", "").strip():
        missing = [v for v in ("PGUSER", "PGPASSWORD", "PGDATABASE") if not os.getenv(v)]
        if missing:
            raise RuntimeError(f"PGHOST is set in .env but these are missing: {', '.join(missing)}")
        query = {}
        if os.getenv("PGSSLMODE"):
            query["sslmode"] = os.environ["PGSSLMODE"]
        if os.getenv("PGCHANNELBINDING"):
            query["channel_binding"] = os.environ["PGCHANNELBINDING"]
        return URL.create(
            DRIVER,
            username=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            host=direct_host(os.environ["PGHOST"].strip()),
            port=int(os.getenv("PGPORT", "5432")),
            database=os.environ["PGDATABASE"],
            query=query,
        )

    missing = [v for v in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"no database configured in .env: set DATABASE_URL (or Neon's PGHOST, PGUSER, "
                           f"PGPASSWORD, PGDATABASE), or for the local database {', '.join(missing)} "
                           "(see docs/docker.md)")
    return URL.create(
        DRIVER,
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.getenv("POSTGRES_HOST", "db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
        # Set explicitly so Neon-style PGSSLMODE / PGCHANNELBINDING = require in .env can't
        # break the local container, which has no SSL.
        query={"sslmode": os.getenv("POSTGRES_SSLMODE", "prefer"), "channel_binding": "prefer"},
    )


def get_engine():
    return create_engine(get_url(), pool_pre_ping=True)
