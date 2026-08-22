from __future__ import annotations

import os
import re
from dataclasses import dataclass

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult
from .router import ContextModelRouter
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
    router: ContextModelRouter | None = None,
    progress=None,
    current_pick: int | None = None,
    next_pick: int | None = None,
    max_deep_dives: int | None = None,
) -> list[AIContextResult]:
    store = store or ContextStore()

    if router is None:
        router = ContextModelRouter(
            luna_model=os.getenv(
                "FANTASY_GM_CONTEXT_MODEL",
                "gpt-5.6-luna",
            ),
            terra_model=os.getenv(
                "FANTASY_GM_DEEP_DIVE_MODEL",
                "gpt-5.6-terra",
            ),
            threshold=float(
                os.getenv(
                    "FANTASY_GM_DEEP_DIVE_THRESHOLD",
                    "0.55",
                )
            ),
        )

    if max_deep_dives is None:
        max_deep_dives = int(
            os.getenv(
                "FANTASY_GM_MAX_DEEP_DIVES_PER_REFRESH",
                "8",
            )
        )

    existing = store.load_all()
    refreshed: list[AIContextResult] = []
    terra_used = 0

    for index, player in enumerate(board[:top], start=1):
        old = existing.get(player.espn_id)

        if (
            old
            and not force
            and store.is_fresh(old, hours=freshness_hours)
        ):
            if progress:
                progress(index, top, player, "cached", None)
            continue

        if progress:
            progress(index, top, player, "luna", None)

        quantitative_context = {
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
            "pick_value": player.pick_value,
            "next_pick_survival": player.next_pick_survival,
            "fandom_bonus": player.fandom_bonus,
        }

        routed = router.research(
            player=player,
            quantitative_context=quantitative_context,
            current_pick=current_pick,
            next_pick=next_pick,
            board_rank=index,
            allow_terra=terra_used < max_deep_dives,
        )

        if routed.terra is not None:
            terra_used += 1
            final = routed.terra
            status = f"terra (deep={routed.escalation.score:.2f})"
        else:
            final = routed.luna

            if (
                routed.escalation.escalate
                and terra_used >= max_deep_dives
            ):
                status = f"luna-budget (deep={routed.escalation.score:.2f})"
            else:
                status = f"luna (deep={routed.escalation.score:.2f})"

        route_note = "; ".join(
            routed.escalation.reasons[:4]
        )

        final.summary = (
            f"{final.summary} "
            f"[route: {status}; {route_note}]"
        ).strip()

        store.put(final)
        refreshed.append(final)

        if progress:
            progress(
                index,
                top,
                player,
                status,
                routed.escalation,
            )

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
            target_id = name_map.get(
                _norm(effect.player_name)
            )

            if target_id is None:
                continue

            if target_id == source.espn_id:
                continue

            weighted = (
                max(-8.0, min(8.0, effect.delta))
                * max(
                    0.0,
                    min(1.0, effect.confidence),
                )
            )

            propagated[target_id] = (
                propagated.get(target_id, 0.0)
                + weighted
            )

            reasons.setdefault(
                target_id,
                [],
            ).append(
                f"{source.player_name}: "
                f"{effect.reason}"
            )

    for player_id in list(propagated):
        propagated[player_id] = max(
            -12.0,
            min(
                12.0,
                propagated[player_id],
            ),
        )

    return ResolvedContext(
        direct=direct,
        propagated_delta=propagated,
        propagated_reasons=reasons,
    )


def _norm(name: str) -> str:
    return re.sub(
        r"[^a-z0-9]",
        "",
        name.lower(),
    )