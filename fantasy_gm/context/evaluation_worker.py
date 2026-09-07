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
from .news_scan import run_news_scan
from .postgres_store import PostgresContextStore
from .service import ContextStoreLike

# Positions worth spending a research call on. Matches what the draft
# board considers draftable, expressed against POSITION_IDS' own labels
# (board/builder.py's DRAFTABLE_POSITIONS uses "DST" instead of "D/ST"
# and so never actually matches a defense — not repeating that here).
EVALUATED_POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}

ProgressCallback = Callable[[int, int], None]


def build_roster_universe(
    client: ESPNClient,
    *,
    team_id: int,
) -> list[BoardPlayer]:
    """Just our own roster — for consumers that only ever need that."""
    roster = load_team_roster(client, team_id)
    return [
        board_player_from_roster_entry(entry) for entry in roster.entries
    ]


def build_evaluator_pool(
    client: ESPNClient,
    *,
    team_id: int,
    free_agent_pool_size: int = 50,
) -> list[BoardPlayer]:
    """
    The Evaluator's own standing inventory: roster + top free agents.

    This is broader than any one task needs (a lineup decision only
    cares about the roster) because the point of the Evaluator is to be
    the shared cache other workers read from instead of re-researching
    players themselves — waivers need free agents compared against the
    roster, so free agents need to already be in the inventory by the
    time a waiver run looks for them.
    """
    candidates: dict[int, BoardPlayer] = {}

    roster = load_team_roster(client, team_id)
    for entry in roster.entries:
        candidates[entry.player_id] = board_player_from_roster_entry(entry)

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

        candidate = board_player_from_pool_entry(entry)
        if candidate is None or candidate.espn_id in candidates:
            continue

        candidates[candidate.espn_id] = candidate

    return list(candidates.values())


def board_player_from_roster_entry(entry: RosterEntry) -> BoardPlayer:
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


def board_player_from_pool_entry(entry: dict) -> BoardPlayer | None:
    """
    Only returns a candidate for an actual free agent in this league.

    get_player_pool() sorts by ESPN-wide ownership percentage, not by
    availability in this specific league — without this check, the
    "top owned" pool is dominated by whoever the biggest global stars
    are (locked on someone's roster in every league they're in), not
    players anyone here could actually add.
    """
    on_team = entry.get("onTeamId")
    if on_team not in (0, None):
        return None

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
    free_agent_pool_size: int = 50,
    freshness_hours: int = 24,
    max_calls: int = 5,
    progress: ProgressCallback | None = None,
) -> list[AIContextResult]:
    """
    Refresh the off-field "feeling" signal for the roster + top free
    agents, in a small, bounded number of calls rather than one call per
    player. On-field, statistical signal (usage trends, etc.) is a
    separate, purely computational concern this does not cover.

    This is the recurring counterpart to context.service.refresh_context
    (which only ever covered the draft board): same store, an ongoing
    rather than one-shot player universe, and now a broad-scan-plus-
    escalation research strategy instead of per-player research calls —
    most players most days have no off-field news at all, so researching
    each individually spent nearly the whole budget on non-findings.
    """
    store = store or PostgresContextStore()

    universe = build_evaluator_pool(
        client,
        team_id=team_id,
        free_agent_pool_size=free_agent_pool_size,
    )

    existing = store.load_all()
    stale = [
        player
        for player in universe
        if not (
            (old := existing.get(player.espn_id))
            and store.is_fresh(old, hours=freshness_hours)
        )
    ]

    if progress:
        progress(len(stale), len(universe))

    results = run_news_scan(
        stale,
        broad_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
        deep_dive_model=os.getenv(
            "FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"
        ),
        max_calls=max_calls,
    )

    store.put_many(results)

    return results


def _float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
