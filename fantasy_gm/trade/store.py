# fantasy_gm/trade/store.py

from __future__ import annotations

import json
import os
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row

from .models import TradeRunResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_decisions (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    team_id BIGINT NOT NULL,
    roster_analysis JSONB NOT NULL,
    partner_candidates JSONB NOT NULL,
    candidate_trades JSONB NOT NULL,
    deep_dives_used JSONB NOT NULL,
    decision JSONB NOT NULL,
    calls_used INTEGER NOT NULL,
    executed BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS trade_decisions_created_at_idx
    ON trade_decisions (created_at DESC);
"""


class TradeDecisionStore:
    """
    Audit trail of every trade pipeline run — mirrors
    waiver.store.WaiverDecisionStore, kept as its own table since a trade
    decision run is shaped differently (partner candidates, bilateral
    offer/request) rather than a single roster's add/drop.
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

    def record(self, *, team_id: int, result: TradeRunResult) -> None:
        insert = """
            INSERT INTO trade_decisions (
                team_id, roster_analysis, partner_candidates,
                candidate_trades, deep_dives_used, decision, calls_used,
                executed
            ) VALUES (
                %(team_id)s, %(roster_analysis)s, %(partner_candidates)s,
                %(candidate_trades)s, %(deep_dives_used)s, %(decision)s,
                %(calls_used)s, %(executed)s
            )
        """

        params = {
            "team_id": team_id,
            "roster_analysis": json.dumps(asdict(result.roster_analysis)),
            "partner_candidates": json.dumps(
                [asdict(p) for p in result.partner_candidates]
            ),
            "candidate_trades": json.dumps(
                [asdict(t) for t in result.candidate_trades]
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
