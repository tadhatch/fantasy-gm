# fantasy_gm/repair/engine.py
#
# Emergency roster repair decisions. Pure and deterministic (no ESPN,
# no database, no LLM): given the roster, the free-agent pool, and the
# league's rules, decide which starting slots can't be covered by anyone
# on the roster and the least damaging legal acquisition for each.

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from fantasy_gm.config import env_float, env_str
from fantasy_gm.lineup.availability import AvailabilityStatus
from fantasy_gm.lineup.models import LineupCandidate
from fantasy_gm.lineup.optimizer import (
    BENCH_SLOT_ID,
    DEFAULT_FILL_ORDER,
    IR_SLOT_ID,
    optimize_lineup,
)
from fantasy_gm.models.roster import RosterEntry

from .models import (
    SLOT_POSITIONS,
    DropOption,
    FreeAgent,
    LeagueRosterRules,
    RepairAction,
    RepairPlan,
    RosterAsset,
    UnrepairableSlot,
)

STRATEGIES = {"best_value", "same_position", "lowest_ros"}
PROVISIONAL_DISCOUNT = 0.15
MAX_ALTERNATIVES_RECORDED = 5


@dataclass(frozen=True, slots=True)
class RepairSettings:
    # best_value: drop the lowest keep-value player anywhere, but prefer
    #   the affected position when its weakest player is within
    #   same_position_margin of the overall weakest.
    # same_position: affected position only (falls back to anywhere if
    #   nobody there is droppable).
    # lowest_ros: lowest raw rest-of-season estimate, ignoring
    #   replaceability.
    strategy: str = "best_value"
    same_position_margin: float = 10.0
    # Minimum estimated roster value change (add ROS + this week's fill -
    # drop ROS) for a drop to be worth it. Protects a valuable,
    # temporarily-unavailable player from being cut for a one-week fill.
    min_value_delta: float = 0.0
    # A waiver claim must clear at least this long before the player's
    # kickoff for him to be set into the lineup in time.
    waiver_lead: timedelta = timedelta(minutes=45)
    # Same for an immediate free-agent add.
    free_agent_lead: timedelta = timedelta(minutes=5)
    # Prefer a free agent over a waiver claim (which may not succeed and
    # costs priority) when their projections are this close.
    waiver_tolerance: float = 1.5

    @classmethod
    def from_env(cls) -> "RepairSettings":
        strategy = env_str("FANTASY_GM_REPAIR_DROP_STRATEGY", "best_value")
        if strategy not in STRATEGIES:
            raise ValueError(
                f"FANTASY_GM_REPAIR_DROP_STRATEGY={strategy!r}; expected "
                f"one of {sorted(STRATEGIES)}"
            )
        return cls(
            strategy=strategy,
            same_position_margin=env_float(
                "FANTASY_GM_REPAIR_SAME_POSITION_MARGIN", 10.0
            ),
            min_value_delta=env_float("FANTASY_GM_REPAIR_MIN_VALUE_DELTA", 0.0),
            waiver_lead=timedelta(
                minutes=env_float("FANTASY_GM_REPAIR_WAIVER_LEAD_MINUTES", 45)
            ),
        )


def plan_repair(
    assets: list[RosterAsset],
    free_agents: list[FreeAgent],
    rules: LeagueRosterRules,
    *,
    now: datetime,
    settings: RepairSettings | None = None,
    pending_claim_slots: list[list[int]] | None = None,
) -> RepairPlan:
    """
    `pending_claim_slots`: eligible slot ids of each player we already
    have a pending waiver claim on -- a slot such a claim would cover is
    marked awaiting, never re-claimed.
    """
    settings = settings or RepairSettings()
    working = list(assets)

    lineup = optimize_lineup([a.candidate for a in working], rules.slot_counts)
    uncovered = {
        slot: n
        for slot, n in lineup.unfilled_slots.items()
        if slot not in (BENCH_SLOT_ID, IR_SLOT_ID) and n > 0
    }
    plan = RepairPlan(uncovered_slots=dict(uncovered))
    if not uncovered:
        return plan

    pending = [list(s) for s in (pending_claim_slots or [])]
    taken: set[int] = set()

    order = [s for s in DEFAULT_FILL_ORDER if s in uncovered]
    order += [s for s in uncovered if s not in order]

    for slot_id in order:
        for _ in range(uncovered[slot_id]):
            claim = next(
                (i for i, slots in enumerate(pending) if slot_id in slots),
                None,
            )
            if claim is not None:
                pending.pop(claim)
                plan.awaiting[slot_id] = plan.awaiting.get(slot_id, 0) + 1
                continue

            if rules.acquisition_blocked:
                plan.unrepairable.append(
                    UnrepairableSlot(slot_id, rules.acquisition_blocked)
                )
                continue

            pool = [fa for fa in free_agents if fa.player_id not in taken]
            action, failure = _repair_slot(
                slot_id, working, pool, free_agents, rules, now, settings
            )
            if action is None:
                plan.unrepairable.append(failure)
                continue

            plan.actions.append(action)
            taken.add(action.add.player_id)
            working = _apply(working, action)

    return plan


def _fa_contributes(
    fa: FreeAgent, slot_id: int, now: datetime, settings: RepairSettings
) -> str | None:
    """None if this free agent can fill `slot_id` this scoring period,
    otherwise why not."""
    if slot_id not in fa.eligible_slot_ids:
        return "not eligible for slot"
    if not fa.availability.can_start:
        return f"unavailable: {fa.availability.reason}"
    if fa.kickoff is None:
        return "no known kickoff this week"
    if fa.on_waivers:
        if fa.waiver_clears_at is None:
            return "on waivers, clear time unknown"
        if fa.waiver_clears_at > fa.kickoff - settings.waiver_lead:
            return (
                f"on waivers until {fa.waiver_clears_at.isoformat()}, too "
                "late for his game"
            )
    elif now > fa.kickoff - settings.free_agent_lead:
        return "his game is about to start or has started"
    return None


def _fa_score(fa: FreeAgent) -> float:
    if fa.availability.status == AvailabilityStatus.PROVISIONAL:
        return fa.weekly_projection * (1 - PROVISIONAL_DISCOUNT)
    return fa.weekly_projection


def _rank_free_agents(
    pool: list[FreeAgent], slot_id: int, now: datetime, settings: RepairSettings
) -> tuple[list[FreeAgent], dict[str, int]]:
    viable: list[FreeAgent] = []
    excluded: dict[str, int] = {}
    for fa in pool:
        why = _fa_contributes(fa, slot_id, now, settings)
        if why is None:
            viable.append(fa)
        elif why != "not eligible for slot":
            key = why.split(":")[0].split(" until ")[0]
            excluded[key] = excluded.get(key, 0) + 1

    viable.sort(key=lambda f: (-_fa_score(f), -f.ros_value, f.player_id))
    if viable:
        # A claim may lose to a higher-priority team and spends priority;
        # take a near-equal free agent instead.
        best = _fa_score(viable[0])
        near = [
            f
            for f in viable
            if not f.on_waivers
            and best - _fa_score(f) <= settings.waiver_tolerance
        ]
        if viable[0].on_waivers and near:
            viable.remove(near[0])
            viable.insert(0, near[0])
    return viable, excluded


def _repair_slot(
    slot_id: int,
    working: list[RosterAsset],
    pool: list[FreeAgent],
    all_free_agents: list[FreeAgent],
    rules: LeagueRosterRules,
    now: datetime,
    settings: RepairSettings,
) -> tuple[RepairAction | None, UnrepairableSlot | None]:
    ranked, excluded = _rank_free_agents(pool, slot_id, now, settings)
    alternatives = [
        {
            "player_id": f.player_id,
            "name": f.name,
            "pool_status": f.pool_status,
            "weekly_projection": round(f.weekly_projection, 1),
            "ros_value_estimate": round(f.ros_value, 1),
            "availability": f.availability.reason,
        }
        for f in ranked[:MAX_ALTERNATIVES_RECORDED]
    ]

    if not ranked:
        return None, UnrepairableSlot(
            slot_id,
            "no available free agent or waiver player can play this week",
            [{"excluded": excluded}],
        )

    last_options: list[dict] = []
    for fa in ranked[:MAX_ALTERNATIVES_RECORDED]:
        action, options = _make_room(
            slot_id, fa, working, all_free_agents, rules, settings
        )
        last_options = options
        if action is not None:
            action.alternatives = alternatives
            return action, None

    return None, UnrepairableSlot(
        slot_id,
        "no legal roster move makes room for any viable replacement",
        [{"alternatives": alternatives, "drop_options": last_options}],
    )


def _position_count(working: list[RosterAsset], position_id) -> int:
    return sum(1 for a in working if a.position_id == position_id)


def _within_position_limit(
    fa: FreeAgent,
    working: list[RosterAsset],
    rules: LeagueRosterRules,
    dropped: RosterAsset | None,
) -> bool:
    limit = rules.position_limits.get(fa.position_id or -1)
    if limit is None or limit <= 0:
        return True
    count = _position_count(working, fa.position_id) + 1
    if dropped is not None and dropped.position_id == fa.position_id:
        count -= 1
    return count <= limit


def _make_room(
    slot_id: int,
    fa: FreeAgent,
    working: list[RosterAsset],
    all_free_agents: list[FreeAgent],
    rules: LeagueRosterRules,
    settings: RepairSettings,
) -> tuple[RepairAction | None, list[dict]]:
    fill_value = fa.ros_value + fa.weekly_projection
    active = [a for a in working if a.candidate.entry.lineup_slot_id != IR_SLOT_ID]

    # 1. Open roster spot: just add.
    if len(active) < rules.roster_max:
        if not _within_position_limit(fa, working, rules, None):
            return None, [{"reason": "position limit reached"}]
        return (
            RepairAction(
                slot_id=slot_id,
                add=fa,
                drop=None,
                ir_player_id=None,
                value_delta=fill_value,
                reason="open roster spot -- no drop needed",
            ),
            [],
        )

    # 2. Open IR slot: stash an IR-eligible injured player instead of
    #    dropping anyone.
    ir_open = rules.slot_counts.get(IR_SLOT_ID, 0) - (len(working) - len(active))
    if ir_open > 0 and _within_position_limit(fa, working, rules, None):
        stashable = [
            a
            for a in active
            if (a.candidate.entry.injury_status or "").upper()
            in rules.ir_eligible_statuses
            and not a.candidate.locked
            and not a.roster_locked
        ]
        if stashable:
            stash = max(stashable, key=lambda a: (a.ros_value, -a.player_id))
            return (
                RepairAction(
                    slot_id=slot_id,
                    add=fa,
                    drop=None,
                    ir_player_id=stash.player_id,
                    value_delta=fill_value,
                    reason=(
                        f"moved {stash.name} to an open IR slot -- no drop "
                        "needed"
                    ),
                ),
                [],
            )

    # 3. Drop someone.
    options = _evaluate_drops(slot_id, fa, working, all_free_agents, rules, settings)
    viable = [o for o in options if o.rejected_reason is None]
    chosen = _choose_drop(viable, slot_id, settings)
    options_out = [o.as_dict() for o in options]
    if chosen is None:
        return None, options_out

    return (
        RepairAction(
            slot_id=slot_id,
            add=fa,
            drop=chosen,
            ir_player_id=None,
            value_delta=fill_value - chosen.ros_value,
            drop_options=options_out,
            reason=_drop_reason(chosen, viable, slot_id, settings),
        ),
        options_out,
    )


def _replacement_baseline(
    position_id, all_free_agents: list[FreeAgent]
) -> float:
    return max(
        (f.ros_value for f in all_free_agents if f.position_id == position_id),
        default=0.0,
    )


def _evaluate_drops(
    slot_id: int,
    fa: FreeAgent,
    working: list[RosterAsset],
    all_free_agents: list[FreeAgent],
    rules: LeagueRosterRules,
    settings: RepairSettings,
) -> list[DropOption]:
    current = optimize_lineup([a.candidate for a in working], rules.slot_counts)
    unfilled_before = sum(current.unfilled_slots.values())
    fa_candidate = _fa_candidate(fa)
    starting_slots = {
        s: n
        for s, n in rules.slot_counts.items()
        if s not in (BENCH_SLOT_ID, IR_SLOT_ID)
    }

    options: list[DropOption] = []
    for asset in working:
        entry = asset.candidate.entry
        if entry.lineup_slot_id == IR_SLOT_ID:
            continue

        keep = max(
            0.0,
            asset.ros_value
            - _replacement_baseline(asset.position_id, all_free_agents),
        )
        option = DropOption(
            player_id=asset.player_id,
            name=asset.name,
            position_id=asset.position_id,
            ros_value=asset.ros_value,
            keep_value=keep,
        )
        options.append(option)

        if asset.roster_locked or asset.candidate.locked:
            option.rejected_reason = "locked -- can't be dropped right now"
            continue
        if not asset.droppable:
            option.rejected_reason = "on the league's undroppable list"
            continue
        if not _within_position_limit(fa, working, rules, asset):
            option.rejected_reason = "would exceed the position limit"
            continue

        remaining = [a.candidate for a in working if a is not asset]
        remaining.append(fa_candidate)

        short = [
            s
            for s, required in starting_slots.items()
            if sum(
                1
                for c in remaining
                if c.entry.lineup_slot_id != IR_SLOT_ID
                and s in c.entry.eligible_slot_ids
            )
            < required
        ]
        if short:
            option.rejected_reason = (
                f"would leave too few rostered players for slot(s) {short}"
            )
            continue

        after = optimize_lineup(remaining, rules.slot_counts)
        if sum(after.unfilled_slots.values()) >= unfilled_before:
            option.rejected_reason = (
                "would open another starting hole this week"
            )
            continue

        delta = fa.ros_value + fa.weekly_projection - asset.ros_value
        if delta < settings.min_value_delta:
            option.rejected_reason = (
                f"costs more roster value than it gains (estimated "
                f"{delta:+.1f})"
            )
            continue

    options.sort(key=lambda o: (o.keep_value, o.ros_value, o.player_id))
    return options


def _choose_drop(
    viable: list[DropOption], slot_id: int, settings: RepairSettings
) -> DropOption | None:
    if not viable:
        return None

    affected = SLOT_POSITIONS.get(slot_id, set())
    same = [o for o in viable if o.position_id in affected]

    if settings.strategy == "lowest_ros":
        return min(viable, key=lambda o: (o.ros_value, o.player_id))

    overall = viable[0]  # sorted by keep_value
    if settings.strategy == "same_position":
        return same[0] if same else overall

    if same and same[0].keep_value <= overall.keep_value + settings.same_position_margin:
        return same[0]
    return overall


def _drop_reason(
    chosen: DropOption,
    viable: list[DropOption],
    slot_id: int,
    settings: RepairSettings,
) -> str:
    affected = SLOT_POSITIONS.get(slot_id, set())
    where = (
        "at the affected position"
        if chosen.position_id in affected
        else "at another position (cheaper than the affected position)"
    )
    return (
        f"drop {chosen.name} {where}: lowest estimated value kept "
        f"({chosen.keep_value:.1f} ROS pts above freely available "
        f"replacements; strategy={settings.strategy})"
    )


def _fa_candidate(fa: FreeAgent) -> LineupCandidate:
    return LineupCandidate(
        entry=RosterEntry(
            player_id=fa.player_id,
            name=fa.name,
            lineup_slot_id=BENCH_SLOT_ID,
            eligible_slot_ids=fa.eligible_slot_ids,
            default_position_id=fa.position_id,
            pro_team_id=fa.pro_team_id,
            injury_status=fa.availability.injury_status,
        ),
        projected_points=_fa_score(fa),
        availability=fa.availability,
        kickoff=fa.kickoff,
    )


def _apply(working: list[RosterAsset], action: RepairAction) -> list[RosterAsset]:
    updated: list[RosterAsset] = []
    for asset in working:
        if action.drop and asset.player_id == action.drop.player_id:
            continue
        if asset.player_id == action.ir_player_id:
            entry = asset.candidate.entry.model_copy(
                update={"lineup_slot_id": IR_SLOT_ID}
            )
            asset = replace(asset, candidate=replace(asset.candidate, entry=entry))
        updated.append(asset)
    updated.append(
        RosterAsset(candidate=_fa_candidate(action.add), ros_value=action.add.ros_value)
    )
    return updated
