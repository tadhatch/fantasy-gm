# fantasy_gm/repair/store.py

from __future__ import annotations

import json
import os

import psycopg
from psycopg.rows import dict_row


SCHEMA = """
CREATE TABLE IF NOT EXISTS roster_repairs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    team_id BIGINT NOT NULL,
    scoring_period INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    uncovered_slots JSONB NOT NULL,
    awaiting JSONB NOT NULL,
    actions JSONB NOT NULL,
    unrepairable JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS roster_repairs_period_idx
    ON roster_repairs (scoring_period, created_at DESC);
"""


class RosterRepairStore:
    """Audit trail of every emergency roster repair run: what was
    uncovered, what was acquired/dropped and why (with the alternatives
    considered), and what couldn't be fixed."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def record(
        self,
        *,
        team_id: int,
        scoring_period: int,
        trigger: str,
        uncovered_slots: dict,
        awaiting: dict,
        actions: list[dict],
        unrepairable: list[dict],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO roster_repairs (
                    team_id, scoring_period, trigger, uncovered_slots,
                    awaiting, actions, unrepairable
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    team_id,
                    scoring_period,
                    trigger,
                    json.dumps({str(k): v for k, v in uncovered_slots.items()}),
                    json.dumps({str(k): v for k, v in awaiting.items()}),
                    json.dumps(actions, default=str),
                    json.dumps(unrepairable, default=str),
                ),
            )
            conn.commit()
