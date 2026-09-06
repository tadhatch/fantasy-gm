# fantasy_gm/waiver/store.py

from __future__ import annotations

import json
import os
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row

from .models import WaiverRunResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS waiver_decisions (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    team_id BIGINT NOT NULL,
    roster_analysis JSONB NOT NULL,
    shortlist JSONB NOT NULL,
    candidate_moves JSONB NOT NULL,
    deep_dives_used JSONB NOT NULL,
    decision JSONB NOT NULL,
    calls_used INTEGER NOT NULL,
    executed BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS waiver_decisions_created_at_idx
    ON waiver_decisions (created_at DESC);
"""


class WaiverDecisionStore:
    """
    Audit trail of every waiver pipeline run — what the roster analysis
    said, who was shortlisted, what was reasoned about, and what the GM
    decision actually was. Separate table from player_evaluations: that
    one is per-player evaluation state, this is a record of an entire
    decision-making run.
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

    def record(self, *, team_id: int, result: WaiverRunResult) -> None:
        insert = """
            INSERT INTO waiver_decisions (
                team_id, roster_analysis, shortlist, candidate_moves,
                deep_dives_used, decision, calls_used, executed
            ) VALUES (
                %(team_id)s, %(roster_analysis)s, %(shortlist)s,
                %(candidate_moves)s, %(deep_dives_used)s, %(decision)s,
                %(calls_used)s, %(executed)s
            )
        """

        params = {
            "team_id": team_id,
            "roster_analysis": json.dumps(asdict(result.roster_analysis)),
            "shortlist": json.dumps([asdict(s) for s in result.shortlist]),
            "candidate_moves": json.dumps(
                [asdict(m) for m in result.candidate_moves]
            ),
            "deep_dives_used": json.dumps(
                [asdict(d) for d in result.deep_dives_used]
            ),
            "decision": json.dumps(asdict(result.decision)),
            "calls_used": result.calls_used,
            "executed": result.executed,
        }

        with self._connect() as conn:
            conn.execute(insert, params)
            conn.commit()
