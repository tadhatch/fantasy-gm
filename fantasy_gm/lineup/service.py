# fantasy_gm/lineup/service.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from fantasy_gm.config import env_int
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.roster import (
    current_scoring_period,
    load_lineup_settings,
    load_team_roster,
)
from fantasy_gm.espn.transactions import ESPNTransactionsClient
from fantasy_gm.models.roster import RosterEntry
from fantasy_gm.nfl_schedule import (
    is_team_on_bye,
    team_kickoff_for_week,
    week_first_kickoff,
)
from fantasy_gm.obs import log_event
from fantasy_gm.valuation.projections import (
    extract_dst_week_projection,
    extract_week_projection,
)

from .availability import (
    AvailabilitySettings,
    AvailabilityStatus,
    evaluate_availability,
)
from .models import LineupCandidate, LineupPlan
from .optimizer import (
    BENCH_SLOT_ID,
    IR_SLOT_ID,
    moves_from_assignments,
    optimize_lineup,
)
from .validator import LineupValidation, validate_assignments

logger = logging.getLogger(__name__)

# Ranking-only discount for a game-time decision who's still provisional
# (pre-inactives). Never affects eligibility -- see availability.py. Once
# he's confirmed active, no discount applies.
PROVISIONAL_DISCOUNT = 0.15

INDIVIDUAL_GAME_LOCK = "INDIVIDUAL_GAME"


@dataclass(slots=True)
class LineupSubmission:
    week: int
    plan: LineupPlan | None
    validation: LineupValidation | None
    # "submitted" | "shadow" | "no_change" | "no_legal_lineup"
    outcome: str
    attempts: int

    @property
    def submitted(self) -> bool:
        return self.outcome == "submitted"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_locked(
    entry: RosterEntry,
    *,
    kickoff: datetime | None,
    on_bye: bool | None,
    week_start: datetime | None,
    lock_type: str,
    now: datetime,
) -> bool:
    """
    ESPN's own lineupLocked flag wins when it says locked. Otherwise the
    schedule decides. When neither can say -- schedule unavailable and
    ESPN didn't report a flag -- the player is treated as LOCKED: not
    moving a movable player costs a little; trying to move a locked one
    gets the whole transaction rejected.
    """
    if entry.lineup_locked is True:
        return True

    lock_at = kickoff if lock_type == INDIVIDUAL_GAME_LOCK else week_start
    if lock_at is not None:
        return now >= lock_at

    if lock_type == INDIVIDUAL_GAME_LOCK and on_bye is True:
        return False  # no game this week, nothing to lock on

    return entry.lineup_locked is not False


def load_lineup_candidates(
    client: ESPNClient,
    *,
    team_id: int,
    week: int,
    now_fn: Callable[[], datetime] = _utcnow,
    with_projections: bool = True,
    context_deltas: dict[int, float] | None = None,
    availability_settings: AvailabilitySettings | None = None,
) -> tuple[dict[int, LineupCandidate], dict[int, int]]:
    """
    Fresh ESPN roster read -> one LineupCandidate per rostered player,
    with availability computed deterministically and stamped with when
    ESPN was read. `with_projections=False` skips the projection fetch
    for pure availability/lock checks (final validation, health checks).
    """
    settings = availability_settings or AvailabilitySettings.from_env()
    season = client.settings.espn_season

    slot_counts, lock_type = load_lineup_settings(client)
    roster = load_team_roster(client, team_id)
    projections = (
        _load_weekly_projections(client, roster.entries, week=week)
        if with_projections
        else {}
    )
    week_start = (
        None
        if lock_type == INDIVIDUAL_GAME_LOCK
        else week_first_kickoff(season, week=week)
    )
    now = now_fn()
    context_deltas = context_deltas or {}

    candidates: dict[int, LineupCandidate] = {}
    for entry in roster.entries:
        on_bye = is_team_on_bye(season, entry.pro_team_id, week=week)
        kickoff = (
            team_kickoff_for_week(season, entry.pro_team_id, week=week)
            if on_bye is False
            else None
        )

        availability = evaluate_availability(
            injury_status=entry.injury_status,
            position_id=entry.default_position_id,
            lineup_slot_id=entry.lineup_slot_id,
            nfl_active=entry.nfl_active,
            on_bye=on_bye,
            kickoff=kickoff,
            observed_at=roster.fetched_at,
            now=now,
            settings=settings,
        )

        raw = projections.get(entry.player_id)
        base = raw or 0.0
        if availability.status == AvailabilityStatus.PROVISIONAL:
            base *= 1 - PROVISIONAL_DISCOUNT
        delta = context_deltas.get(entry.player_id, 0.0)

        candidates[entry.player_id] = LineupCandidate(
            entry=entry,
            projected_points=base + delta,
            availability=availability,
            locked=_is_locked(
                entry,
                kickoff=kickoff,
                on_bye=on_bye,
                week_start=week_start,
                lock_type=lock_type,
                now=now,
            ),
            raw_projection=raw,
            evaluation_delta=delta,
            kickoff=kickoff,
        )

    return candidates, slot_counts


def _load_context_deltas() -> dict[int, float]:
    """
    Cached real-world evaluation deltas. Optional input: if Postgres is
    unreachable the lineup still gets set from ESPN data alone.
    """
    try:
        from fantasy_gm.context.postgres_store import PostgresContextStore

        return {
            pid: ctx.weighted_delta()
            for pid, ctx in PostgresContextStore().load_all().items()
        }
    except Exception as exc:
        log_event(
            logger,
            "lineup.context_unavailable",
            level=logging.WARNING,
            error=repr(exc),
        )
        return {}


def build_lineup_plan(
    client: ESPNClient,
    *,
    team_id: int,
    week: int | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
    context_deltas: dict[int, float] | None = None,
) -> tuple[LineupPlan, int, dict[int, int]]:
    week = week if week is not None else current_scoring_period(client)
    if context_deltas is None:
        context_deltas = _load_context_deltas()

    candidates, slot_counts = load_lineup_candidates(
        client,
        team_id=team_id,
        week=week,
        now_fn=now_fn,
        context_deltas=context_deltas,
    )
    plan = optimize_lineup(list(candidates.values()), slot_counts)
    return plan, week, slot_counts


def submit_lineup_safely(
    client: ESPNClient,
    *,
    team_id: int,
    week: int | None = None,
    confirm: bool = False,
    trigger: str = "scheduled",
    max_attempts: int | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
    txn: ESPNTransactionsClient | None = None,
    decision_store=None,
    context_deltas: dict[int, float] | None = None,
) -> LineupSubmission:
    """
    The only path by which an automated lineup reaches ESPN.

    Each attempt: build a plan from fresh ESPN data -> validate it ->
    re-read ESPN again and validate the same assignments against that
    fresh read -> submit exactly the moves that passed. Any failure
    discards the plan and rebuilds from scratch on the next attempt.
    If no attempt produces a valid lineup, nothing is submitted and a
    CRITICAL event is logged.

    Unfilled starting slots (every eligible rostered player unavailable)
    don't block submission -- the lineup is still legal, just short --
    but are logged CRITICAL so roster repair can act on them.
    """
    week = week if week is not None else current_scoring_period(client)
    max_attempts = max_attempts or env_int(
        "FANTASY_GM_LINEUP_MAX_REPAIR_ATTEMPTS", 3
    )
    if context_deltas is None:
        context_deltas = _load_context_deltas()

    plan: LineupPlan | None = None
    validation: LineupValidation | None = None
    approved = False
    attempt = 0

    try:
        for attempt in range(1, max_attempts + 1):
            plan, _, slot_counts = build_lineup_plan(
                client,
                team_id=team_id,
                week=week,
                now_fn=now_fn,
                context_deltas=context_deltas,
            )
            validation = validate_assignments(
                plan.assignments, plan.candidates, slot_counts
            )
            if not validation.ok:
                log_event(
                    logger,
                    "lineup.validation_rejected",
                    level=logging.ERROR,
                    stage="proposed",
                    attempt=attempt,
                    trigger=trigger,
                    errors=validation.errors,
                )
                continue

            fresh, fresh_counts = load_lineup_candidates(
                client,
                team_id=team_id,
                week=week,
                now_fn=now_fn,
                with_projections=False,
            )
            validation = validate_assignments(
                plan.assignments, fresh, fresh_counts
            )
            if not validation.ok:
                log_event(
                    logger,
                    "lineup.validation_rejected",
                    level=logging.ERROR,
                    stage="final_fresh_read",
                    attempt=attempt,
                    trigger=trigger,
                    errors=validation.errors,
                )
                continue

            validation.moves = tuple(
                moves_from_assignments(plan.assignments, fresh)
            )
            approved = True
            break

        if not approved:
            log_event(
                logger,
                "lineup.no_legal_lineup",
                level=logging.CRITICAL,
                week=week,
                trigger=trigger,
                attempts=attempt,
                errors=validation.errors if validation else [],
                action="nothing submitted; check roster manually",
            )
            outcome = "no_legal_lineup"
        else:
            outcome = _submit(
                client,
                team_id=team_id,
                week=week,
                confirm=confirm,
                trigger=trigger,
                plan=plan,
                validation=validation,
                txn=txn,
            )

        submission = LineupSubmission(
            week=week,
            plan=plan,
            validation=validation,
            outcome=outcome,
            attempts=attempt,
        )
        _record(submission, team_id=team_id, trigger=trigger, store=decision_store)
        return submission
    finally:
        _mark_lineup_attempt()


def _submit(
    client: ESPNClient,
    *,
    team_id: int,
    week: int,
    confirm: bool,
    trigger: str,
    plan: LineupPlan,
    validation: LineupValidation,
    txn: ESPNTransactionsClient | None,
) -> str:
    for slot_id, missing in plan.unfilled_slots.items():
        log_event(
            logger,
            "lineup.slot_unfilled",
            level=logging.CRITICAL,
            week=week,
            slot_id=slot_id,
            missing=missing,
            action="roster repair needed -- no startable rostered player",
        )
    for warning in validation.warnings:
        log_event(logger, "lineup.validation_warning", week=week, detail=warning)

    if not validation.moves:
        log_event(
            logger,
            "lineup.validated",
            week=week,
            trigger=trigger,
            moves=0,
            outcome="no_change",
        )
        return "no_change"

    txn = txn or ESPNTransactionsClient(client)
    response = txn.set_lineup(
        team_id=team_id,
        moves=list(validation.moves),
        scoring_period_id=week,
        confirm=confirm,
        validation=validation,
    )
    outcome = "submitted" if response is not None else "shadow"

    for move in validation.moves:
        candidate = plan.candidates.get(move.player_id)
        log_event(
            logger,
            "lineup.move",
            week=week,
            player=candidate.entry.name if candidate else move.player_id,
            from_slot=move.from_slot_id,
            to_slot=move.to_slot_id,
            projected=(
                round(candidate.projected_points, 2) if candidate else None
            ),
            availability=(
                candidate.availability.reason if candidate else None
            ),
            outcome=outcome,
        )
    log_event(
        logger,
        "lineup.validated",
        week=week,
        trigger=trigger,
        moves=len(validation.moves),
        revalidate=validation.revalidate_player_ids,
        outcome=outcome,
    )
    return outcome


def _record(
    submission: LineupSubmission, *, team_id: int, trigger: str, store
) -> None:
    try:
        if store is None:
            from .store import LineupDecisionStore

            store = LineupDecisionStore()

        plan = submission.plan
        validation = submission.validation
        candidates = []
        if plan is not None:
            for pid, c in plan.candidates.items():
                candidates.append(
                    {
                        "player_id": pid,
                        "name": c.entry.name,
                        "from_slot": c.entry.lineup_slot_id,
                        "to_slot": plan.assignments.get(
                            pid, c.entry.lineup_slot_id
                        ),
                        "projected_points": c.projected_points,
                        "raw_projection": c.raw_projection,
                        "evaluation_delta": c.evaluation_delta,
                        "locked": c.locked,
                        "kickoff": c.kickoff.isoformat() if c.kickoff else None,
                        "availability": c.availability.as_dict(),
                    }
                )

        store.record(
            team_id=team_id,
            scoring_period=submission.week,
            trigger=trigger,
            outcome=submission.outcome,
            attempts=submission.attempts,
            moves=[
                {
                    "player_id": m.player_id,
                    "from_slot": m.from_slot_id,
                    "to_slot": m.to_slot_id,
                }
                for m in (validation.moves if validation else ())
            ],
            candidates=candidates,
            validation={
                "errors": validation.errors if validation else [],
                "warnings": validation.warnings if validation else [],
                "revalidate_player_ids": (
                    validation.revalidate_player_ids if validation else []
                ),
            },
            notes=plan.notes if plan else [],
        )
    except Exception:
        # Audit trail only -- never block a lineup on it.
        logger.exception("lineup: failed to record decision")


def _mark_lineup_attempt() -> None:
    try:
        from fantasy_gm.railway.task_runs import TaskRunStore

        TaskRunStore().mark_run("lineup")
    except Exception:
        logger.exception("lineup: failed to record task run")


def find_lineup_problems(
    candidates: dict[int, LineupCandidate],
    slot_counts: dict[int, int],
) -> list[str]:
    """
    Cheap, deterministic "does the current lineup need attention right
    now?" check for the supervisor: an unlocked starter who can't start,
    a short starting slot with a startable, unlocked bench player who
    could fill it, or a short slot nobody rostered can fill (roster
    repair). Returns a stable list of problem keys (empty if
    fine).
    """
    problems: list[str] = []
    filled: dict[int, int] = {}

    for pid, c in candidates.items():
        slot = c.entry.lineup_slot_id
        if slot in (BENCH_SLOT_ID, IR_SLOT_ID):
            continue
        filled[slot] = filled.get(slot, 0) + 1
        if not c.locked and not c.available:
            problems.append(f"starter:{pid}")

    for slot_id, required in slot_counts.items():
        if slot_id in (BENCH_SLOT_ID, IR_SLOT_ID):
            continue
        if filled.get(slot_id, 0) >= required:
            continue
        has_fill = any(
            c.entry.lineup_slot_id == BENCH_SLOT_ID
            and not c.locked
            and c.available
            and slot_id in c.entry.eligible_slot_ids
            for c in candidates.values()
        )
        if has_fill:
            problems.append(f"empty_slot:{slot_id}")
        else:
            # Nobody on the roster can fill it -- the lineup worker's
            # roster repair step needs to run.
            problems.append(f"uncovered_slot:{slot_id}")

    return sorted(problems)


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
