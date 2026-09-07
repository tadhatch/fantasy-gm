# fantasy_gm/lineup/service.py

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fantasy_gm.context.postgres_store import PostgresContextStore
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.roster import (
    current_scoring_period,
    load_roster_slot_counts,
    load_team_roster,
)
from fantasy_gm.espn.transactions import ESPNTransactionsClient
from fantasy_gm.models.roster import RosterEntry
from fantasy_gm.nfl_schedule import is_team_locked, is_team_on_bye
from fantasy_gm.valuation.projections import (
    extract_dst_week_projection,
    extract_week_projection,
)

from .models import LineupCandidate, LineupPlan
from .optimizer import optimize_lineup

logger = logging.getLogger(__name__)

# OUT/IR/suspended players score 0 regardless of any projection — they
# never get chosen over an available alternative. DOUBTFUL/QUESTIONABLE
# stay eligible but discounted, since a clearly-best questionable player
# is still usually the right start in practice.
UNAVAILABLE_INJURY_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENDED"}
RISKY_INJURY_DISCOUNTS = {"DOUBTFUL": 0.6, "QUESTIONABLE": 0.15}


def build_lineup_plan(
    client: ESPNClient,
    *,
    team_id: int,
    week: int | None = None,
) -> tuple[LineupPlan, int]:
    week = week if week is not None else current_scoring_period(client)

    roster = load_team_roster(client, team_id)
    slot_counts = load_roster_slot_counts(client)

    projections = _load_weekly_projections(
        client, roster.entries, week=week
    )
    cached_context = PostgresContextStore().load_all()
    now = datetime.now(timezone.utc)

    season = client.settings.espn_season

    candidates: list[LineupCandidate] = []
    for entry in roster.entries:
        weekly_points = projections.get(entry.player_id)
        available, adjusted, reason = _evaluate_availability(
            entry, weekly_points, season=season, week=week
        )

        evaluation_delta = 0.0
        ctx = cached_context.get(entry.player_id)
        if ctx is not None:
            evaluation_delta = ctx.weighted_delta()
            adjusted += evaluation_delta

        locked = is_team_locked(
            season, entry.pro_team_id, week=week, now=now
        )

        candidates.append(
            LineupCandidate(
                entry=entry,
                projected_points=adjusted,
                available=available,
                unavailable_reason=reason,
                locked=locked,
                raw_projection=weekly_points,
                evaluation_delta=evaluation_delta,
            )
        )

    plan = optimize_lineup(candidates, slot_counts)

    try:
        from fantasy_gm.railway.task_runs import TaskRunStore

        TaskRunStore().mark_run("lineup")
    except Exception:
        # Bookkeeping only — never block a real lineup decision on it.
        logger.exception("lineup: failed to record task run")

    return plan, week


def submit_lineup_plan(
    client: ESPNClient,
    *,
    team_id: int,
    plan: LineupPlan,
    week: int,
    confirm: bool = False,
) -> dict | None:
    if not plan.moves:
        return None

    txn = ESPNTransactionsClient(client)
    return txn.set_lineup(
        team_id=team_id,
        moves=plan.moves,
        scoring_period_id=week,
        confirm=confirm,
    )


def _load_weekly_projections(
    client: ESPNClient,
    entries: list[RosterEntry],
    *,
    week: int,
) -> dict[int, float | None]:
    player_ids = [entry.player_id for entry in entries]
    dst_ids = {
        entry.player_id for entry in entries if entry.default_position_id == 16
    }
    pool_entries = client.get_players_by_id(
        player_ids, scoring_period_id=week
    )

    projections: dict[int, float | None] = {}
    for entry in pool_entries:
        player = entry.get("player") or entry
        espn_id = entry.get("id") or player.get("id")
        if espn_id is None:
            continue
        espn_id = int(espn_id)

        if espn_id in dst_ids:
            projections[espn_id] = extract_dst_week_projection(
                player, season=client.settings.espn_season, week=week
            )
        else:
            projections[espn_id] = extract_week_projection(
                entry, season=client.settings.espn_season, week=week
            )

    return projections


def _evaluate_availability(
    entry: RosterEntry,
    weekly_points: float | None,
    *,
    season: int,
    week: int,
) -> tuple[bool, float, str | None]:
    # Checked against the real schedule first, not inferred from a
    # missing projection — a missing projection could be a bye, but
    # could also just be missing data, and those deserve different
    # reasons shown to whoever's reading this.
    on_bye = is_team_on_bye(season, entry.pro_team_id, week=week)
    if on_bye is True:
        return False, 0.0, "on bye this week"

    if weekly_points is None:
        return False, 0.0, "no projection this week"

    status = (entry.injury_status or "").upper()
    if status in UNAVAILABLE_INJURY_STATUSES:
        return False, weekly_points, f"injury status {status}"

    discount = RISKY_INJURY_DISCOUNTS.get(status, 0.0)
    adjusted = weekly_points * (1 - discount)
    reason = f"injury status {status}" if discount else None
    return True, adjusted, reason
