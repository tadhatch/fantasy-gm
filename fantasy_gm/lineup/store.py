# fantasy_gm/lineup/store.py

from __future__ import annotations

import json
import os

import psycopg
from psycopg.rows import dict_row


SCHEMA = """
CREATE TABLE IF NOT EXISTS lineup_decisions (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    team_id BIGINT NOT NULL,
    scoring_period INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    outcome TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    moves JSONB NOT NULL,
    candidates JSONB NOT NULL,
    validation JSONB NOT NULL,
    notes JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS lineup_decisions_period_idx
    ON lineup_decisions (scoring_period, created_at DESC);
"""


class LineupDecisionStore:
    """
    Audit trail of every lineup gate run: what was proposed, each
    candidate's availability (with its source and timestamp), what the
    validator said, and whether anything reached ESPN. Before this,
    lineup decisions weren't persisted at all, so a bad start couldn't
    be explained after the fact.
    """

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
        outcome: str,
        attempts: int,
        moves: list[dict],
        candidates: list[dict],
        validation: dict,
        notes: list[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lineup_decisions (
                    team_id, scoring_period, trigger, outcome, attempts,
                    moves, candidates, validation, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    team_id,
                    scoring_period,
                    trigger,
                    outcome,
                    attempts,
                    json.dumps(moves),
                    json.dumps(candidates, default=str),
                    json.dumps(validation, default=str),
                    json.dumps(notes),
                ),
            )
            conn.commit()
