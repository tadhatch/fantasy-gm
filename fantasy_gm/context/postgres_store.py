# fantasy_gm/context/postgres_store.py

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from .models import AIContextResult, EvidenceItem, RelatedPlayerEffect


SCHEMA = """
CREATE TABLE IF NOT EXISTS player_evaluations (
    id BIGSERIAL PRIMARY KEY,
    espn_id BIGINT NOT NULL,
    player_name TEXT NOT NULL,
    researched_at TIMESTAMPTZ NOT NULL,
    model TEXT NOT NULL,
    summary TEXT NOT NULL,
    direct_delta DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    category TEXT NOT NULL,
    availability_risk DOUBLE PRECISION NOT NULL DEFAULT 0,
    distraction_risk DOUBLE PRECISION NOT NULL DEFAULT 0,
    role_change DOUBLE PRECISION NOT NULL DEFAULT 0,
    injury_change DOUBLE PRECISION NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL DEFAULT '[]',
    related_players JSONB NOT NULL DEFAULT '[]',
    raw_response_id TEXT
);

CREATE INDEX IF NOT EXISTS player_evaluations_espn_id_researched_at_idx
    ON player_evaluations (espn_id, researched_at DESC);
"""


class PostgresContextStore:
    """
    Durable, shared replacement for the file-based ContextStore.

    Every worker this project runs is an ephemeral Railway container that
    starts fresh from the built image and disappears when the job ends —
    a local file (the original ContextStore's `.fantasy-gm/context.json`)
    can't accumulate evaluations across those runs or be shared between
    the evaluation worker and whatever reads its output (lineup, waiver,
    trade). This writes to Postgres instead, one row per research pass so
    history is preserved, with `load_all`/`get` returning the latest row
    per player — same read interface as ContextStore.
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

    def load_all(self) -> dict[int, AIContextResult]:
        query = """
            SELECT DISTINCT ON (espn_id) *
            FROM player_evaluations
            ORDER BY espn_id, researched_at DESC
        """
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()

        return {row["espn_id"]: self._decode(row) for row in rows}

    def latest_researched_at(self) -> datetime | None:
        """
        When the most recent evaluation of any player was written. Used
        to decide whether a new evaluation run is due, without needing
        to track "last dispatched" in the supervisor's own memory — which
        would reset (and could re-trigger unnecessarily) on every
        restart.
        """
        query = "SELECT MAX(researched_at) AS latest FROM player_evaluations"

        with self._connect() as conn:
            row = conn.execute(query).fetchone()

        return row["latest"] if row else None

    def notable_recent(
        self,
        *,
        hours: int = 48,
        limit: int = 8,
        min_abs_delta: float = 2.0,
    ) -> list[AIContextResult]:
        """
        The handful of evaluations from the last `hours` worth actually
        surfacing somewhere like chat — recent and a large enough delta
        (either direction) to be worth mentioning, biggest impact first.
        Not the whole cache: most players most days have nothing notable
        and shouldn't show up here.
        """
        all_recent = self.load_all()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        notable = []
        for result in all_recent.values():
            if abs(result.direct_delta) < min_abs_delta:
                continue
            try:
                researched = datetime.fromisoformat(
                    result.researched_at.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if researched < cutoff:
                continue
            notable.append(result)

        notable.sort(key=lambda r: abs(r.direct_delta), reverse=True)
        return notable[:limit]

    def get(self, espn_id: int) -> AIContextResult | None:
        query = """
            SELECT *
            FROM player_evaluations
            WHERE espn_id = %s
            ORDER BY researched_at DESC
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(query, (espn_id,)).fetchone()

        return self._decode(row) if row else None

    def put(self, value: AIContextResult) -> None:
        self.put_many([value])

    def put_many(self, values: list[AIContextResult]) -> None:
        if not values:
            return

        insert = """
            INSERT INTO player_evaluations (
                espn_id, player_name, researched_at, model, summary,
                direct_delta, confidence, category, availability_risk,
                distraction_risk, role_change, injury_change, evidence,
                related_players, raw_response_id
            ) VALUES (
                %(espn_id)s, %(player_name)s, %(researched_at)s, %(model)s,
                %(summary)s, %(direct_delta)s, %(confidence)s, %(category)s,
                %(availability_risk)s, %(distraction_risk)s,
                %(role_change)s, %(injury_change)s, %(evidence)s,
                %(related_players)s, %(raw_response_id)s
            )
        """

        with self._connect() as conn:
            for value in values:
                conn.execute(insert, self._encode(value))
            conn.commit()

    def is_fresh(self, value: AIContextResult, hours: int = 12) -> bool:
        try:
            dt = datetime.fromisoformat(
                value.researched_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        return datetime.now(timezone.utc) - dt <= timedelta(hours=hours)

    def _encode(self, value: AIContextResult) -> dict:
        return {
            "espn_id": value.espn_id,
            "player_name": value.player_name,
            "researched_at": value.researched_at,
            "model": value.model,
            "summary": value.summary,
            "direct_delta": value.direct_delta,
            "confidence": value.confidence,
            "category": value.category,
            "availability_risk": value.availability_risk,
            "distraction_risk": value.distraction_risk,
            "role_change": value.role_change,
            "injury_change": value.injury_change,
            "evidence": json.dumps(
                [_asdict(e) for e in value.evidence]
            ),
            "related_players": json.dumps(
                [_asdict(r) for r in value.related_players]
            ),
            "raw_response_id": value.raw_response_id,
        }

    def _decode(self, row: dict) -> AIContextResult:
        researched_at = row["researched_at"]
        if isinstance(researched_at, datetime):
            researched_at = researched_at.isoformat()

        evidence = row.get("evidence") or []
        related = row.get("related_players") or []

        return AIContextResult(
            espn_id=int(row["espn_id"]),
            player_name=row["player_name"],
            researched_at=researched_at,
            model=row["model"],
            summary=row.get("summary", ""),
            direct_delta=float(row.get("direct_delta", 0.0)),
            confidence=float(row.get("confidence", 0.0)),
            category=row.get("category", "news"),
            availability_risk=float(row.get("availability_risk", 0.0)),
            distraction_risk=float(row.get("distraction_risk", 0.0)),
            role_change=float(row.get("role_change", 0.0)),
            injury_change=float(row.get("injury_change", 0.0)),
            evidence=[EvidenceItem(**e) for e in evidence],
            related_players=[RelatedPlayerEffect(**r) for r in related],
            raw_response_id=row.get("raw_response_id"),
        )


def _asdict(value) -> dict:
    return {
        field: getattr(value, field)
        for field in value.__dataclass_fields__
    }
