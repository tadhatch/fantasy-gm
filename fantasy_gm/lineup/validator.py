# fantasy_gm/lineup/validator.py
#
# The deterministic gate every lineup must pass immediately before it's
# submitted to ESPN. Pure Python, independent of the LLM and of how the
# lineup was produced (optimizer, manual CLI moves, post-transaction
# repair) -- it only looks at the proposed assignments against freshly
# read roster/availability data.

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from fantasy_gm.espn.constants import LINEUP_SLOT_IDS
from fantasy_gm.espn.transactions import LineupMove

from .models import LineupCandidate
from .optimizer import BENCH_SLOT_ID, IR_SLOT_ID

NON_STARTING_SLOTS = {BENCH_SLOT_ID, IR_SLOT_ID}

# ESPN's default IR-slot rule; leagues can differ, so it's configurable.
DEFAULT_IR_ELIGIBLE_STATUSES = "INJURY_RESERVE,OUT"


def ir_eligible_statuses_from_env() -> set[str]:
    from fantasy_gm.config import env_str

    raw = env_str(
        "FANTASY_GM_IR_ELIGIBLE_STATUSES", DEFAULT_IR_ELIGIBLE_STATUSES
    )
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


@dataclass(slots=True)
class LineupValidation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Starters who may play but must be rechecked after inactives.
    revalidate_player_ids: list[int] = field(default_factory=list)
    # The exact moves this validation approved -- the transactions
    # client refuses to submit any other set (see
    # ESPNTransactionsClient.set_lineup).
    moves: tuple[LineupMove, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _slot(slot_id: int) -> str:
    return str(LINEUP_SLOT_IDS.get(slot_id, slot_id))


def validate_assignments(
    assignments: dict[int, int],
    candidates: dict[int, LineupCandidate],
    slot_counts: dict[int, int],
    *,
    ir_eligible_statuses: set[str] | None = None,
) -> LineupValidation:
    """
    `assignments` maps player id -> proposed slot id; any rostered
    player missing from it is assumed to stay where he is. `candidates`
    must be built from fresh ESPN data -- this is the final check.
    """
    result = LineupValidation()

    for pid in assignments:
        if pid not in candidates:
            result.errors.append(
                f"player {pid} is assigned a slot but is no longer on "
                "the roster"
            )

    starters_per_slot: Counter[int] = Counter()
    ir_moves_in = ir_moves_out = 0
    if ir_eligible_statuses is None:
        ir_eligible_statuses = ir_eligible_statuses_from_env()

    for pid, candidate in candidates.items():
        name = candidate.entry.name
        current = candidate.entry.lineup_slot_id
        target = assignments.get(pid, current)

        if candidate.locked and target != current:
            result.errors.append(
                f"{name} is locked in {_slot(current)} and can't be "
                f"moved to {_slot(target)}"
            )
            continue

        if target in NON_STARTING_SLOTS:
            if target == IR_SLOT_ID and current != IR_SLOT_ID:
                ir_moves_in += 1
                status = (candidate.entry.injury_status or "").upper()
                if status not in ir_eligible_statuses:
                    result.errors.append(
                        f"{name} ({status or 'no status'}) isn't eligible "
                        "for the IR slot"
                    )
            elif target == BENCH_SLOT_ID and current == IR_SLOT_ID:
                ir_moves_out += 1
            continue

        starters_per_slot[target] += 1

        if target not in candidate.entry.eligible_slot_ids:
            result.errors.append(
                f"{name} isn't eligible for {_slot(target)}"
            )

        availability = candidate.availability
        if not availability.can_start:
            message = (
                f"{name} can't start at {_slot(target)}: "
                f"{availability.reason}"
            )
            if candidate.locked:
                # Already locked in -- nothing we can change now, but it
                # must be visible.
                result.warnings.append(f"{message} (locked, can't move)")
            else:
                result.errors.append(message)
        elif availability.requires_revalidation:
            result.revalidate_player_ids.append(pid)
            result.warnings.append(
                f"{name} starts provisionally at {_slot(target)} "
                f"({availability.reason})"
            )

    for slot_id, count in starters_per_slot.items():
        allowed = slot_counts.get(slot_id, 0)
        if count > allowed:
            result.errors.append(
                f"{count} players assigned to {_slot(slot_id)}, league "
                f"allows {allowed}"
            )

    ir_occupied = sum(
        1 for c in candidates.values() if c.entry.lineup_slot_id == IR_SLOT_ID
    )
    if ir_moves_in and (
        ir_occupied - ir_moves_out + ir_moves_in
        > slot_counts.get(IR_SLOT_ID, 0)
    ):
        result.errors.append(
            "not enough open IR slots for the requested IR move(s)"
        )

    for slot_id, required in slot_counts.items():
        if slot_id in NON_STARTING_SLOTS:
            continue
        filled = starters_per_slot.get(slot_id, 0)
        if filled < required:
            result.warnings.append(
                f"{_slot(slot_id)} is short {required - filled} "
                "starter(s) -- roster repair needed"
            )

    return result
