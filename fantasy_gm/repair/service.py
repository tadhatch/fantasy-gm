# fantasy_gm/repair/service.py
#
# ESPN-facing side of emergency roster repair: gathers the roster, the
# free-agent pool, league rules, and our own pending claims; hands them
# to the pure engine (engine.plan_repair); then executes the resulting
# moves through the same shadow-first transactions client as everything
# else, and re-runs the lineup availability gate afterwards.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from fantasy_gm.config import env_bool
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.roster import current_scoring_period
from fantasy_gm.espn.transactions import ESPNTransactionsClient
from fantasy_gm.lineup.availability import (
    AvailabilitySettings,
    evaluate_availability,
)
from fantasy_gm.lineup.optimizer import IR_SLOT_ID, optimize_lineup
from fantasy_gm.lineup.service import load_lineup_candidates, submit_lineup_safely
from fantasy_gm.lineup.validator import (
    ir_eligible_statuses_from_env,
    validate_assignments,
)
from fantasy_gm.nfl_schedule import is_team_on_bye, team_kickoff_for_week
from fantasy_gm.obs import log_event
from fantasy_gm.valuation.projections import (
    extract_dst_week_projection,
    extract_ros_projection,
    extract_season_actual,
    extract_week_projection,
)

from .engine import RepairSettings, plan_repair
from .models import FreeAgent, LeagueRosterRules, RepairAction, RepairPlan, RosterAsset

logger = logging.getLogger(__name__)

DST_POSITION_ID = 16
FREE_AGENT_POOL_LIMIT = 150
PER_SLOT_POOL_LIMIT = 50


@dataclass(slots=True)
class RepairOutcome:
    week: int
    plan: RepairPlan
    # player id added -> "submitted" | "claim_pending" | "shadow" | "failed: ..."
    results: dict[int, str] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_roster_repair(
    client: ESPNClient,
    *,
    team_id: int,
    week: int | None = None,
    confirm: bool | None = None,
    trigger: str = "lineup_worker",
    now_fn: Callable[[], datetime] = _utcnow,
    txn: ESPNTransactionsClient | None = None,
    store=None,
    settings: RepairSettings | None = None,
) -> RepairOutcome:
    """
    `confirm` defaults to FANTASY_GM_REPAIR_CONFIRM -- the same double
    gate as every other transaction: nothing is submitted unless that is
    true AND FANTASY_GM_TRANSACTIONS_MODE=live.
    """
    week = week if week is not None else current_scoring_period(client)
    if confirm is None:
        confirm = env_bool("FANTASY_GM_REPAIR_CONFIRM", False)
    settings = settings or RepairSettings.from_env()
    season = client.settings.espn_season

    candidates, slot_counts = load_lineup_candidates(
        client, team_id=team_id, week=week, now_fn=now_fn
    )
    uncovered = {
        s: n
        for s, n in optimize_lineup(
            list(candidates.values()), slot_counts
        ).unfilled_slots.items()
        if n > 0
    }
    if not uncovered:
        return RepairOutcome(week=week, plan=RepairPlan(uncovered_slots={}))

    log_event(
        logger,
        "repair.started",
        level=logging.WARNING,
        week=week,
        trigger=trigger,
        uncovered_slots=uncovered,
    )

    rules, remaining_weeks = _league_rules(client, team_id, slot_counts, week)
    assets = _roster_assets(client, candidates, week, season, remaining_weeks)
    free_agents = _free_agents(
        client, list(uncovered), week, season, remaining_weeks, now_fn
    )
    pending = _pending_claim_slots(client, team_id)

    plan = plan_repair(
        assets,
        free_agents,
        rules,
        now=now_fn(),
        settings=settings,
        pending_claim_slots=pending,
    )

    outcome = RepairOutcome(week=week, plan=plan)
    txn = txn or ESPNTransactionsClient(client)
    any_immediate_add = False

    for action in plan.actions:
        result = _execute(
            client, txn, action, team_id=team_id, week=week,
            confirm=confirm, candidates=candidates, slot_counts=slot_counts,
        )
        outcome.results[action.add.player_id] = result
        if result == "submitted":
            any_immediate_add = True
        log_event(
            logger,
            "repair.action",
            week=week,
            slot_id=action.slot_id,
            add=action.add.name,
            add_pool_status=action.add.pool_status,
            drop=action.drop.name if action.drop else None,
            ir_player_id=action.ir_player_id,
            value_delta_estimate=round(action.value_delta, 1),
            result=result,
            reason=action.reason,
        )

    for slot_id, count in plan.awaiting.items():
        log_event(
            logger,
            "repair.awaiting_waiver",
            week=week,
            slot_id=slot_id,
            count=count,
        )

    for failure in plan.unrepairable:
        log_event(
            logger,
            "repair.unrepairable",
            level=logging.CRITICAL,
            week=week,
            slot_id=failure.slot_id,
            reason=failure.reason,
            details=failure.details,
            action="manual roster move needed",
        )

    if any_immediate_add:
        # The add landed on the bench; put him in the lineup through the
        # same availability gate as every lineup change.
        try:
            submit_lineup_safely(
                client,
                team_id=team_id,
                week=week,
                confirm=env_bool("FANTASY_GM_LINEUP_CONFIRM", False),
                trigger="post_roster_repair",
                now_fn=now_fn,
            )
        except Exception:
            logger.exception("repair: post-repair lineup gate failed")

    _record(outcome, team_id=team_id, trigger=trigger, store=store)
    return outcome


def _execute(
    client: ESPNClient,
    txn: ESPNTransactionsClient,
    action: RepairAction,
    *,
    team_id: int,
    week: int,
    confirm: bool,
    candidates,
    slot_counts: dict[int, int],
) -> str:
    try:
        if action.ir_player_id is not None:
            # Judge only the IR move itself (the player + IR capacity),
            # not unrelated lineup state.
            ir_scope = {
                pid: c
                for pid, c in candidates.items()
                if pid == action.ir_player_id
                or c.entry.lineup_slot_id == IR_SLOT_ID
            }
            validation = validate_assignments(
                {action.ir_player_id: IR_SLOT_ID}, ir_scope, slot_counts
            )
            if not validation.ok:
                return f"failed: IR move rejected: {validation.errors}"
            moves = _ir_moves(action.ir_player_id, candidates)
            validation.moves = tuple(moves)
            ir_response = txn.set_lineup(
                team_id=team_id,
                moves=moves,
                scoring_period_id=week,
                confirm=confirm,
                validation=validation,
            )
            if ir_response is None:
                # Shadow: the IR move didn't happen, so neither can an
                # add that depends on the freed roster spot.
                txn.add_drop(
                    team_id=team_id,
                    add_player_id=action.add.player_id,
                    drop_player_id=None,
                    scoring_period_id=week,
                    via_waiver=action.add.on_waivers,
                    confirm=False,
                )
                return "shadow"

        response = txn.add_drop(
            team_id=team_id,
            add_player_id=action.add.player_id,
            drop_player_id=action.drop.player_id if action.drop else None,
            scoring_period_id=week,
            via_waiver=action.add.on_waivers,
            confirm=confirm,
        )
    except Exception as exc:
        # One attempt per run, never retried here -- the next lineup
        # checkpoint/health check re-plans from fresh data.
        logger.exception("repair: transaction failed")
        return f"failed: {exc!r}"

    if response is None:
        return "shadow"
    return "claim_pending" if action.add.on_waivers else "submitted"


def _ir_moves(player_id: int, candidates):
    from fantasy_gm.espn.transactions import LineupMove

    return [
        LineupMove(
            player_id=player_id,
            from_slot_id=candidates[player_id].entry.lineup_slot_id,
            to_slot_id=IR_SLOT_ID,
        )
    ]


def _league_rules(
    client: ESPNClient, team_id: int, slot_counts: dict[int, int], week: int
) -> tuple[LeagueRosterRules, int]:
    data = client.get_league(["mSettings", "mTeam", "mStatus"])
    settings = data.get("settings", {})
    roster_settings = settings.get("rosterSettings", {})
    acquisition = settings.get("acquisitionSettings", {})

    position_limits = {
        int(pos): int(limit)
        for pos, limit in (roster_settings.get("positionLimits") or {}).items()
        if int(limit) > 0
    }
    roster_max = sum(
        count for slot, count in slot_counts.items() if slot != IR_SLOT_ID
    )

    blocked = None
    team = next(
        (t for t in data.get("teams", []) if t.get("id") == team_id), {}
    )
    counter = team.get("transactionCounter") or {}
    season_limit = acquisition.get("acquisitionLimit", -1)
    if season_limit is not None and season_limit >= 0:
        if counter.get("acquisitions", 0) >= season_limit:
            blocked = f"season acquisition limit reached ({season_limit})"
    matchup_limit = acquisition.get("matchupAcquisitionLimit", -1)
    matchup_period = (data.get("status") or {}).get("currentMatchupPeriod")
    if (
        blocked is None
        and matchup_limit is not None
        and matchup_limit >= 0
        and matchup_period is not None
    ):
        used = (counter.get("matchupAcquisitionTotals") or {}).get(
            str(matchup_period), 0
        )
        if used >= matchup_limit:
            blocked = f"matchup acquisition limit reached ({int(matchup_limit)})"

    final_week = (settings.get("scheduleSettings") or {}).get(
        "matchupPeriodCount"
    ) or 17
    remaining_weeks = max(1, int(final_week) - week + 1)

    return (
        LeagueRosterRules(
            slot_counts=slot_counts,
            position_limits=position_limits,
            roster_max=roster_max,
            ir_eligible_statuses=ir_eligible_statuses_from_env(),
            acquisition_blocked=blocked,
        ),
        remaining_weeks,
    )


def _ros_value(
    entry: dict, *, position_id, week: int, season: int, remaining_weeks: int
) -> tuple[float, float]:
    """
    (weekly projection, ROS estimate) for one ESPN player entry. ROS is
    ESPN's season projection minus actual points when ESPN has one;
    otherwise (always for D/ST) a per-week basis times weeks remaining,
    where the basis is this week's projection -- or, when that's zero
    (bye, one-week absence), his season-to-date per-game average, so a
    bye week alone never makes a player look worthless. Estimates, not
    sourced statistics.
    """
    player = entry.get("player") or entry
    is_dst = position_id == DST_POSITION_ID
    if is_dst:
        weekly = extract_dst_week_projection(player, season=season, week=week)
        ros = None  # ESPN's D/ST appliedTotal is always 0
    else:
        weekly = extract_week_projection(entry, season=season, week=week)
        ros = extract_ros_projection(entry, season=season)
    weekly = weekly or 0.0
    if ros is None:
        basis = weekly
        if basis <= 0:
            actual = extract_season_actual(entry, season=season, dst=is_dst)
            basis = (actual or 0.0) / max(1, week - 1)
        ros = basis * remaining_weeks
    return weekly, ros


def _roster_assets(
    client: ESPNClient, candidates, week: int, season: int, remaining_weeks: int
) -> list[RosterAsset]:
    entries = {
        int(e.get("id") or (e.get("player") or {}).get("id")): e
        for e in client.get_players_by_id(
            list(candidates), scoring_period_id=week
        )
    }
    assets = []
    for pid, candidate in candidates.items():
        _, ros = _ros_value(
            entries.get(pid, {}),
            position_id=candidate.entry.default_position_id,
            week=week,
            season=season,
            remaining_weeks=remaining_weeks,
        )
        assets.append(
            RosterAsset(
                candidate=candidate,
                ros_value=ros,
                roster_locked=bool(candidate.entry.roster_locked),
                droppable=candidate.entry.droppable is not False,
            )
        )
    return assets


def _free_agents(
    client: ESPNClient,
    uncovered_slots: list[int],
    week: int,
    season: int,
    remaining_weeks: int,
    now_fn: Callable[[], datetime],
) -> list[FreeAgent]:
    raw: dict[int, dict] = {}
    # Broad pool: replacement baselines for every position.
    for e in client.get_free_agents(
        scoring_period_id=week, limit=FREE_AGENT_POOL_LIMIT
    ):
        raw[int(e["id"])] = e
    # Targeted pool: candidates for the uncovered slots specifically.
    for e in client.get_free_agents(
        scoring_period_id=week,
        slot_ids=uncovered_slots,
        limit=PER_SLOT_POOL_LIMIT,
    ):
        raw[int(e["id"])] = e

    observed_at = now_fn()
    availability_settings = AvailabilitySettings.from_env()

    free_agents = []
    for pid, e in raw.items():
        player = e.get("player") or {}
        position_id = player.get("defaultPositionId")
        pro_team_id = player.get("proTeamId")
        on_bye = is_team_on_bye(season, pro_team_id, week=week)
        kickoff = (
            team_kickoff_for_week(season, pro_team_id, week=week)
            if on_bye is False
            else None
        )
        weekly, ros = _ros_value(
            e,
            position_id=position_id,
            week=week,
            season=season,
            remaining_weeks=remaining_weeks,
        )
        process_ms = e.get("waiverProcessDate")
        free_agents.append(
            FreeAgent(
                player_id=pid,
                name=player.get("fullName", f"ESPN {pid}"),
                position_id=position_id,
                eligible_slot_ids=player.get("eligibleSlots", []),
                pro_team_id=pro_team_id,
                pool_status=str(e.get("status") or "FREEAGENT"),
                availability=evaluate_availability(
                    injury_status=player.get("injuryStatus"),
                    position_id=position_id,
                    lineup_slot_id=None,
                    nfl_active=player.get("active"),
                    on_bye=on_bye,
                    kickoff=kickoff,
                    observed_at=observed_at,
                    now=observed_at,
                    settings=availability_settings,
                ),
                weekly_projection=weekly,
                ros_value=ros,
                kickoff=kickoff,
                waiver_clears_at=(
                    datetime.fromtimestamp(process_ms / 1000, tz=timezone.utc)
                    if process_ms
                    else None
                ),
            )
        )
    return free_agents


def _pending_claim_slots(client: ESPNClient, team_id: int) -> list[list[int]]:
    """Eligible slots of every player we already have a pending claim
    on, so a slot one would cover isn't claimed for twice."""
    data = client.get_league(["mTransactions2"])
    pending_ids = [
        item["playerId"]
        for txn in data.get("transactions", [])
        if txn.get("isPending")
        and txn.get("teamId") == team_id
        and txn.get("type") in ("WAIVER", "FREEAGENT")
        for item in txn.get("items", [])
        if item.get("type") == "ADD" and "playerId" in item
    ]
    if not pending_ids:
        return []
    return [
        (e.get("player") or {}).get("eligibleSlots", [])
        for e in client.get_players_by_id(pending_ids)
    ]


def _record(outcome: RepairOutcome, *, team_id: int, trigger: str, store) -> None:
    try:
        if store is None:
            from .store import RosterRepairStore

            store = RosterRepairStore()
        plan = outcome.plan
        store.record(
            team_id=team_id,
            scoring_period=outcome.week,
            trigger=trigger,
            uncovered_slots=plan.uncovered_slots,
            awaiting=plan.awaiting,
            actions=[
                {**a.as_dict(), "result": outcome.results.get(a.add.player_id)}
                for a in plan.actions
            ],
            unrepairable=[
                {"slot_id": u.slot_id, "reason": u.reason, "details": u.details}
                for u in plan.unrepairable
            ],
        )
    except Exception:
        logger.exception("repair: failed to record run")
