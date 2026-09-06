# fantasy_gm/context/evaluation_worker.py

from __future__ import annotations

import os
from typing import Callable

from fantasy_gm.board.models import BoardPlayer
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import POSITION_IDS
from fantasy_gm.espn.roster import load_team_roster
from fantasy_gm.models.roster import RosterEntry

from .models import AIContextResult
from .postgres_store import PostgresContextStore
from .router import ContextModelRouter
from .service import ContextStoreLike

# Positions worth spending a research call on. Matches what the draft
# board considers draftable, expressed against POSITION_IDS' own labels
# (board/builder.py's DRAFTABLE_POSITIONS uses "DST" instead of "D/ST"
# and so never actually matches a defense — not repeating that here).
EVALUATED_POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}

ProgressCallback = Callable[[int, int, BoardPlayer, str], None]


def build_player_universe(
    client: ESPNClient,
    *,
    team_id: int,
    free_agent_pool_size: int = 50,
) -> list[BoardPlayer]:
    """
    Everyone this run should consider researching: the whole roster (so
    lineup/trade decisions have current context on every player we
    actually own) plus the highest-ownership players league-wide (so
    waiver targets and other teams' trade-relevant assets get covered
    too, not just our own roster).
    """
    candidates: dict[int, BoardPlayer] = {}

    roster = load_team_roster(client, team_id)
    for entry in roster.entries:
        candidates[entry.player_id] = _from_roster_entry(entry)

    roster_count = len(candidates)

    pool_data = client.get_player_pool(
        limit=max(500, free_agent_pool_size * 4)
    )
    pool = (
        pool_data.get("players", [])
        if isinstance(pool_data, dict)
        else pool_data
    )

    for entry in pool:
        if len(candidates) - roster_count >= free_agent_pool_size:
            break

        candidate = _from_pool_entry(entry)
        if candidate is None or candidate.espn_id in candidates:
            continue

        candidates[candidate.espn_id] = candidate

    return list(candidates.values())


def _from_roster_entry(entry: RosterEntry) -> BoardPlayer:
    position = POSITION_IDS.get(
        entry.default_position_id, str(entry.default_position_id or "?")
    )
    return BoardPlayer(
        espn_id=entry.player_id,
        name=entry.name,
        position=position,
        projected_points=0.0,
        replacement_points=0.0,
        vor=0.0,
        nfl_team_id=None,
        injury_status=entry.injury_status,
    )


def _from_pool_entry(entry: dict) -> BoardPlayer | None:
    player = entry.get("player") or entry
    pos_id = player.get("defaultPositionId")
    position = POSITION_IDS.get(pos_id, str(pos_id or "?"))
    if position not in EVALUATED_POSITIONS:
        return None

    espn_id = entry.get("id") or player.get("id")
    if espn_id is None:
        return None

    pool_entry = entry.get("playerPoolEntry") or {}
    ownership = pool_entry.get("ownership") or player.get("ownership") or {}

    return BoardPlayer(
        espn_id=int(espn_id),
        name=player.get("fullName") or f"ESPN {espn_id}",
        position=position,
        projected_points=0.0,
        replacement_points=0.0,
        vor=0.0,
        nfl_team_id=player.get("proTeamId"),
        injury_status=player.get("injuryStatus"),
        percent_owned=_float(ownership.get("percentOwned")),
    )


def evaluate_players(
    client: ESPNClient,
    *,
    team_id: int,
    store: ContextStoreLike | None = None,
    router: ContextModelRouter | None = None,
    freshness_hours: int = 24,
    free_agent_pool_size: int = 50,
    max_research_calls: int = 20,
    max_deep_dives: int | None = None,
    progress: ProgressCallback | None = None,
) -> list[AIContextResult]:
    """
    Refresh real-world evaluations for the roster + top free agents.

    This is the recurring counterpart to context.service.refresh_context
    (which only ever covered the draft board): same research pipeline and
    store, different — and ongoing, not one-shot — player universe. It's
    meant to run on a schedule, so it's deliberately budget-capped rather
    than trying to fully refresh everything every time; freshness
    checking means most of that budget goes to genuinely stale or new
    players once the cache has warmed up.
    """
    store = store or PostgresContextStore()

    if router is None:
        router = ContextModelRouter(
            luna_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
            terra_model=os.getenv(
                "FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"
            ),
            threshold=float(
                os.getenv("FANTASY_GM_DEEP_DIVE_THRESHOLD", "0.55")
            ),
        )

    if max_deep_dives is None:
        max_deep_dives = int(
            os.getenv("FANTASY_GM_MAX_DEEP_DIVES_PER_REFRESH", "8")
        )

    universe = build_player_universe(
        client,
        team_id=team_id,
        free_agent_pool_size=free_agent_pool_size,
    )

    existing = store.load_all()
    refreshed: list[AIContextResult] = []
    terra_used = 0
    calls_made = 0

    for player in universe:
        if calls_made >= max_research_calls:
            break

        old = existing.get(player.espn_id)
        if old and store.is_fresh(old, hours=freshness_hours):
            if progress:
                progress(calls_made, len(universe), player, "cached")
            continue

        quantitative_context = {
            "position": player.position,
            "injury_status_espn": player.injury_status,
            "percent_owned": player.percent_owned,
        }

        routed = router.research(
            player=player,
            quantitative_context=quantitative_context,
            allow_terra=terra_used < max_deep_dives,
        )

        calls_made += 1

        if routed.terra is not None:
            terra_used += 1
            status = f"terra (deep={routed.escalation.score:.2f})"
        else:
            status = f"luna (deep={routed.escalation.score:.2f})"

        store.put(routed.final)
        refreshed.append(routed.final)

        if progress:
            progress(calls_made, len(universe), player, status)

    return refreshed


def _float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
