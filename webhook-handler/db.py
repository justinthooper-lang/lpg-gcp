"""Database connection for the LPG webhook handler.

Connects to Cloud SQL Postgres via the Cloud SQL Auth Proxy listening
on 127.0.0.1:5432. Uses pg8000 (pure-Python driver) and reads the
postgres user's password from the PGPASSWORD environment variable.

For Layer 4 / Cloud Run, this module will be replaced with one using
the google-cloud-sql-connector library with IAM authentication. The
public surface (get_connection()) stays the same so call sites don't
change.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pg8000.dbapi

# Connection config — local dev via Cloud SQL Auth Proxy
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "lpg")
DB_USER = os.getenv("DB_USER", "postgres")


def _password() -> str:
    """Read postgres password from env. Raise clearly if unset."""
    pw = os.getenv("PGPASSWORD")
    if not pw:
        raise RuntimeError(
            "PGPASSWORD environment variable is not set. "
            "Set it in the shell where uvicorn runs."
        )
    return pw


@contextmanager
def get_connection() -> Iterator[pg8000.dbapi.Connection]:
    """Yield a pg8000 DB connection; close on exit.

    Use as a context manager:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    Auto-commits on clean exit, rolls back on exception. pg8000
    connections do NOT auto-commit by default; we commit explicitly
    here so write-paths don't need to remember.
    """
    conn = pg8000.dbapi.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=_password(),
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        