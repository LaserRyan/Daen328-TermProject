"""Shared Postgres connection, built from .env.

If DATABASE_URL is set (our Neon database), it is used as is. Otherwise the URL is built
from the POSTGRES_* variables (the local Postgres container in docker-compose).
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


def get_url():
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        # Neon hands out postgresql://...; make the psycopg2 driver explicit for SQLAlchemy.
        url = make_url(database_url.replace("postgres://", "postgresql://", 1))
        return url.set(drivername="postgresql+psycopg2")

    missing = [v for v in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"missing in .env: {', '.join(missing)} (copy .env.sample to .env, "
                           "or set DATABASE_URL)")
    return URL.create(
        "postgresql+psycopg2",
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
