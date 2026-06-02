"""Database connection for the LPG webhook handler.

Uses google-cloud-sql-python-connector to connect to Cloud SQL Postgres.
The connector library handles platform differences automatically:
- On Cloud Run: connects via Unix socket / private path
- Locally: connects through the Cloud SQL Auth Proxy on 127.0.0.1:5432

Currently uses password authentication (PGPASSWORD env var). Future
work: switch to IAM database auth, which removes the password
entirely. That change is local to this module — call sites don't move.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pg8000.dbapi
from google.cloud.sql.connector import Connector

# Cloud SQL instance connection name: project:region:instance
INSTANCE_CONNECTION_NAME = os.getenv(
    "INSTANCE_CONNECTION_NAME",
    "lpg-dev-496820:us-west1:lpg-dev",
)
DB_NAME = os.getenv("DB_NAME", "lpg")
DB_USER = os.getenv("DB_USER", "postgres")

# When running locally with the Cloud SQL Auth Proxy listening on
# 127.0.0.1:5432, we can either go through the proxy (TCP) or use the
# connector (which also goes through the proxy under the hood). The
# connector path works everywhere, so we use it uniformly.
#
# Set USE_CONNECTOR=false to fall back to direct TCP via the proxy
# (useful only if debugging the connector itself).
USE_CONNECTOR = os.getenv("USE_CONNECTOR", "true").lower() == "true"

_connector: Connector | None = None


def _password() -> str:
    """Read postgres password from env. Raise clearly if unset."""
    pw = os.getenv("PGPASSWORD")
    if not pw:
        raise RuntimeError(
            "PGPASSWORD environment variable is not set. "
            "Set it in the shell where uvicorn runs."
        )
    return pw


def _get_connector() -> Connector:
    """Lazily create the singleton Connector instance."""
    global _connector
    if _connector is None:
        _connector = Connector()
    return _connector


def _connect_via_connector() -> pg8000.dbapi.Connection:
    """Connect using google-cloud-sql-python-connector."""
    return _get_connector().connect(
        INSTANCE_CONNECTION_NAME,
        "pg8000",
        user=DB_USER,
        password=_password(),
        db=DB_NAME,
    )


def _connect_via_tcp() -> pg8000.dbapi.Connection:
    """Direct pg8000 over TCP — assumes a proxy is running on 127.0.0.1:5432.

    Only used if USE_CONNECTOR=false. Kept for debugging.
    """
    return pg8000.dbapi.connect(
        host="127.0.0.1",
        port=5432,
        database=DB_NAME,
        user=DB_USER,
        password=_password(),
    )


@contextmanager
def get_connection() -> Iterator[pg8000.dbapi.Connection]:
    """Yield a pg8000 DB connection; close on exit.

    Use as a context manager:
        with get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
            finally:
                cur.close()

    Auto-commits on clean exit, rolls back on exception.
    """
    if USE_CONNECTOR:
        conn = _connect_via_connector()
    else:
        conn = _connect_via_tcp()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        