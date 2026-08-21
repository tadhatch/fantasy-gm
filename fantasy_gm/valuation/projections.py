from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ESPNProjection:
    projected_points: float
    raw_stats: dict[str, float]
    source: str


def extract_season_projection(entry: dict[str, Any], season: int) -> ESPNProjection | None:
    """
    Extract ESPN's league-context season projection.

    ESPN has used slightly different nesting/field names across fantasy views,
    so this intentionally accepts:
      entry.player.stats
      entry.playerPoolEntry.stats
      entry.stats

    A projection row is identified by statSourceId/statTypeId == 1, seasonId
    matching our season, and preferably scoringPeriodId == 0.
    """
    candidates: list[dict[str, Any]] = []

    player = entry.get("player") or {}
    pool = entry.get("playerPoolEntry") or {}

    for container in (player, pool, entry):
        stats = container.get("stats")
        if isinstance(stats, list):
            candidates.extend(s for s in stats if isinstance(s, dict))

    projected: list[dict[str, Any]] = []
    for stat in candidates:
        if stat.get("seasonId") not in (None, season):
            continue

        source_id = stat.get("statSourceId")
        type_id = stat.get("statTypeId")
        if source_id == 1 or type_id == 1:
            projected.append(stat)

    if not projected:
        return None

    # Whole-season projections are usually scoringPeriodId 0.
    projected.sort(
        key=lambda s: (
            0 if s.get("scoringPeriodId") == 0 else 1,
            -(float(s.get("appliedTotal") or 0.0)),
        )
    )
    best = projected[0]

    raw = best.get("stats") or best.get("appliedStats") or {}
    raw_stats = {
        str(k): float(v)
        for k, v in raw.items()
        if isinstance(v, (int, float))
    }

    applied = best.get("appliedTotal")
    if applied is None:
        # Some ESPN responses put the league-calculated total one level up.
        applied = pool.get("appliedStatTotal")

    if applied is None:
        return None

    return ESPNProjection(
        projected_points=float(applied),
        raw_stats=raw_stats,
        source="espn_league_projection",
    )
