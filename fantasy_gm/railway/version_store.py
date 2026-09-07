# fantasy_gm/railway/version_store.py

from __future__ import annotations

import os

import psycopg


SCHEMA = """
CREATE TABLE IF NOT EXISTS service_versions (
    service_name TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class ServiceVersionStore:
    """
    Tracks which version of a service's provisioning (start command,
    variables, etc. — whatever only gets applied at creation time, not
    on every redeploy) the supervisor last actually deployed.

    Needed because a plain redeploy only picks up new code; a service
    that already exists never gets its variables or start command
    touched again (see ensure_chatbot()). Backed by Postgres rather than
    in-memory state so it survives a supervisor restart — the same
    reasoning as latest_researched_at() for the evaluation worker.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def get(self, service_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM service_versions "
                "WHERE service_name = %s",
                (service_name,),
            ).fetchone()

        return row[0] if row else None

    def set(self, service_name: str, version: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_versions (service_name, version)
                VALUES (%s, %s)
                ON CONFLICT (service_name)
                DO UPDATE SET version = EXCLUDED.version, updated_at = now()
                """,
                (service_name, version),
            )
            conn.commit()
