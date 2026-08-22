"""
Replace the existing refresh_context() in fantasy_gm/context/service.py with the
implementation below, and add the imports:

    import os
    from .router import ContextModelRouter

This version keeps the existing cache behavior while routing fresh research through
Luna -> optional Terra.
"""

from __future__ import annotations

import os

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult
from .router import ContextModelRouter
from .store import ContextStore


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
            luna_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
            terra_model=os.getenv("FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"),
            threshold=float(os.getenv("FANTASY_GM_DEEP_DIVE_THRESHOLD", "0.55")),
        )

    if max_deep_dives is None:
        max_deep_dives = int(os.getenv("FANTASY_GM_MAX_DEEP_DIVES_PER_REFRESH", "8"))

    existing = store.load_all()
    refreshed: list[AIContextResult] = []
    terra_used = 0

    for index, player in enumerate(board[:top], start=1):
        old = existing.get(player.espn_id)

        if old and not force and store.is_fresh(old, hours=freshness_hours):
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
        )

        # Router may recommend Terra, but the per-refresh budget is authoritative.
        if routed.terra is not None:
            if terra_used >= max_deep_dives:
                final = routed.luna
                status = (
                    f"luna-budget "
                    f"(deep={routed.escalation.score:.2f})"
                )
            else:
                terra_used += 1
                final = routed.terra
                status = (
                    f"terra "
                    f"(deep={routed.escalation.score:.2f})"
                )
        else:
            final = routed.final
            status = f"luna (deep={routed.escalation.score:.2f})"

        # Keep routing metadata inside the summary cache without changing the
        # AIContextResult schema.
        route_note = "; ".join(routed.escalation.reasons[:4])
        final.summary = (
            f"{final.summary} [route: {status}; {route_note}]"
        ).strip()

        store.put(final)
        refreshed.append(final)

        if progress:
            progress(index, top, player, status, routed.escalation)

    return refreshed
