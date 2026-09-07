# fantasy_gm/railway/task_runs.py

from __future__ import annotations

import os
from datetime import datetime

import psycopg


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    task_name TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class TaskRunStore:
    """
    Generic "when did this recurring task last actually run" tracker,
    backed by Postgres so it survives a supervisor restart — the same
    reasoning as PostgresContextStore.latest_researched_at() for the
    evaluation worker, generalized for any scheduled task (lineup,
    waiver, trade, ...) rather than building a one-off version of this
    for each one.
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

    def last_run_at(self, task_name: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_run_at FROM task_runs WHERE task_name = %s",
                (task_name,),
            ).fetchone()

        return row[0] if row else None

    def mark_run(self, task_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (task_name, last_run_at)
                VALUES (%s, now())
                ON CONFLICT (task_name)
                DO UPDATE SET last_run_at = now()
                """,
                (task_name,),
            )
            conn.commit()
