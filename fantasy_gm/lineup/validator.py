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
                result.errors.append(
                    f"{name}: moving a player into IR isn't a lineup "
                    "change this gate allows"
                )
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
