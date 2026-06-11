"""PostgreSQL connection helpers for Supabase transaction pooler."""

from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL


POOLER_HOST_SUFFIX = ".pooler.supabase.com"
POOLER_PORT = 6543


def _parsed_database_url():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL in .env")

    parsed = urlparse(DATABASE_URL)
    if not parsed.hostname or not parsed.hostname.endswith(POOLER_HOST_SUFFIX):
        raise RuntimeError("DATABASE_URL must use the Supabase transaction pooler host")
    if parsed.port != POOLER_PORT:
        raise RuntimeError("DATABASE_URL must use Supabase transaction pooler port 6543")

    return parsed


def _database_dsn() -> str:
    parsed = _parsed_database_url()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    return urlunparse(parsed._replace(query=urlencode(query)))


def database_target() -> dict:
    parsed = _parsed_database_url()
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "database": parsed.path.lstrip("/") or "postgres",
        "pooler": parsed.hostname.endswith(POOLER_HOST_SUFFIX) and parsed.port == POOLER_PORT,
    }


@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(
        _database_dsn(),
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    )
    try:
        yield conn
    finally:
        conn.close()
