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


def build_roster_universe(
    client: ESPNClient,
    *,
    team_id: int,
) -> list[BoardPlayer]:
    """
    Just our own roster. This worker's job is keeping our players' real-
    world context fresh for lineup decisions — the player universe a
    task needs depends on the task: waivers need free agents compared
    against the roster (waiver/pipeline.py does its own screening of
    those), trades need other teams' rosters compared against ours. A
    single blanket "roster + top-owned players league-wide" universe
    doesn't serve any of those well — it mostly wastes research calls on
    players nobody here could actually add or is deciding about.
    """
    roster = load_team_roster(client, team_id)
    return [
        board_player_from_roster_entry(entry) for entry in roster.entries
    ]


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
    router: ContextModelRouter | None = None,
    freshness_hours: int = 24,
    max_research_calls: int = 20,
    max_deep_dives: int | None = None,
    progress: ProgressCallback | None = None,
) -> list[AIContextResult]:
    """
    Refresh real-world evaluations for the roster.

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

    universe = build_roster_universe(client, team_id=team_id)

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
