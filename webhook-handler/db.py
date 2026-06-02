"""Database connection for the LPG webhook handler.

Uses google-cloud-sql-python-connector to connect to Cloud SQL Postgres.

Two auth modes:
- **IAM database auth** when running on Cloud Run (detected via K_SERVICE
  env var). The Connector authenticates as the runtime service account
  using short-lived OAuth tokens. No password.
- **Password auth** locally. PGPASSWORD env var, postgres user.

ADR-0012 documents this decision. The mode switch is automatic; call
sites of get_connection() don't change.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pg8000.dbapi
from google.cloud.sql.connector import Connector

INSTANCE_CONNECTION_NAME = os.getenv(
    "INSTANCE_CONNECTION_NAME",
    "lpg-dev-496820:us-west1:lpg-dev",
)
DB_NAME = os.getenv("DB_NAME", "lpg")

# Auth-mode selection:
# - On Cloud Run, K_SERVICE is automatically set; we use IAM auth with
#   the runtime service account.
# - Locally, we use password auth via PGPASSWORD env var.
RUNNING_ON_CLOUD_RUN = bool(os.getenv("K_SERVICE"))

# Service account email (without ".gserviceaccount.com" — Cloud SQL's
# IAM auth quirk). For non-default service accounts we'd configure
# this via env var.
IAM_USER = os.getenv(
    "IAM_DB_USER",
    "388123220900-compute@developer",
)

# Local-dev fallback user.
LOCAL_USER = os.getenv("DB_USER", "postgres")

_connector: Connector | None = None


def _password() -> str:
    """Read postgres password from env. Raise clearly if unset.

    Only used in local-dev (password auth) mode. Cloud Run uses IAM
    tokens and doesn't call this.
    """
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


def _connect() -> pg8000.dbapi.Connection:
    """Open a Cloud SQL connection in the appropriate auth mode."""
    if RUNNING_ON_CLOUD_RUN:
        # IAM database auth — Connector fetches OAuth tokens from the
        # runtime metadata server. No password.
        return _get_connector().connect(
            INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=IAM_USER,
            db=DB_NAME,
            enable_iam_auth=True,
        )
    else:
        # Local dev: password auth as the postgres user.
        return _get_connector().connect(
            INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=LOCAL_USER,
            password=_password(),
            db=DB_NAME,
        )


@contextmanager
def get_connection() -> Iterator[pg8000.dbapi.Connection]:
    """Yield a pg8000 DB connection; close on exit.

    Auto-commits on clean exit, rolls back on exception.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()