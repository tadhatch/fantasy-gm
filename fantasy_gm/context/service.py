from __future__ import annotations

import re
from dataclasses import dataclass

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult
from .researcher import OpenAIContextResearcher
from .store import ContextStore


@dataclass(slots=True)
class ResolvedContext:
    direct: dict[int, AIContextResult]
    propagated_delta: dict[int, float]
    propagated_reasons: dict[int, list[str]]


def refresh_context(
    board: list[BoardPlayer],
    *,
    top: int = 50,
    force: bool = False,
    freshness_hours: int = 12,
    store: ContextStore | None = None,
    researcher: OpenAIContextResearcher | None = None,
    progress=None,
) -> list[AIContextResult]:
    store = store or ContextStore()
    researcher = researcher or OpenAIContextResearcher()

    existing = store.load_all()
    refreshed: list[AIContextResult] = []

    for index, player in enumerate(board[:top], start=1):
        old = existing.get(player.espn_id)
        if old and not force and store.is_fresh(old, hours=freshness_hours):
            if progress:
                progress(index, top, player, "cached")
            continue

        if progress:
            progress(index, top, player, "researching")

        result = researcher.research(
            espn_id=player.espn_id,
            player_name=player.name,
            position=player.position,
            nfl_team=player.nfl_team_id,
            quantitative_context={
                "projected_points": player.projected_points,
                "vor": player.vor,
                "ecr": player.ecr,
                "adp": player.adp,
                "snap_share_2025": player.snap_share,
                "target_share_2025": player.target_share,
                "rush_share_2025": player.rush_share,
                "depth_rank_2026": player.depth_rank,
                "injury_status_espn": player.injury_status,
                "base_value": player.board_score,
            },
        )
        store.put(result)
        refreshed.append(result)

    return refreshed


def resolve_context(
    player_pool: list[BoardPlayer],
    store: ContextStore | None = None,
) -> ResolvedContext:
    store = store or ContextStore()
    direct = store.load_all()

    name_map: dict[str, int] = {}
    for player in player_pool:
        name_map[_norm(player.name)] = player.espn_id

    propagated: dict[int, float] = {}
    reasons: dict[int, list[str]] = {}

    for source in direct.values():
        for effect in source.related_players:
            target_id = name_map.get(_norm(effect.player_name))
            if target_id is None or target_id == source.espn_id:
                continue

            weighted = max(-8.0, min(8.0, effect.delta)) * max(
                0.0, min(1.0, effect.confidence)
            )
            propagated[target_id] = propagated.get(target_id, 0.0) + weighted
            reasons.setdefault(target_id, []).append(
                f"{source.player_name}: {effect.reason}"
            )

    # Cap accumulated propagation so a cluster of articles does not swamp the board.
    for player_id in list(propagated):
        propagated[player_id] = max(-12.0, min(12.0, propagated[player_id]))

    return ResolvedContext(
        direct=direct,
        propagated_delta=propagated,
        propagated_reasons=reasons,
    )


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())
