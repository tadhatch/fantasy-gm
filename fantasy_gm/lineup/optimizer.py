# fantasy_gm/lineup/optimizer.py

from __future__ import annotations

from fantasy_gm.espn.constants import LINEUP_SLOT_IDS
from fantasy_gm.espn.transactions import LineupMove

from .models import LineupCandidate, LineupPlan

BENCH_SLOT_ID = 20
IR_SLOT_ID = 21

# Fill strict (near-single-eligibility) slots before flexible ones, so a
# flex slot doesn't "steal" the only option for a stricter slot. RB/WR
# go before FLEX for the same reason even though most RB/WR are
# themselves also flex-eligible. Any slot id this league has that isn't
# in this list (e.g. a second flex, a superflex) gets appended after it
# — still after the strict positions, just in whatever order it's found.
DEFAULT_FILL_ORDER = [0, 6, 16, 17, 2, 4, 23]  # QB, TE, D/ST, K, RB, WR, FLEX


def optimize_lineup(
    candidates: list[LineupCandidate],
    slot_counts: dict[int, int],
) -> LineupPlan:
    """
    Greedy slot-by-slot fill, not a full optimal assignment solver: for
    each slot (strict positions first, flex-type slots last), take the
    best remaining eligible player(s) by projected points. This is
    provably correct for the common case (every player single-position
    eligible except for flex slots) and only a simplification in the
    rare case of a genuinely multi-position-eligible non-flex player —
    an acceptable trade-off for something this auditable and easy to
    reason about when it's submitting real transactions.

    Only players whose availability says they can start
    (candidate.available) are ever placed in a starting slot -- an
    unavailable player is never started as a "fallback", however high
    his projection. If no startable player exists for a slot it's left
    unfilled (recorded in unfilled_slots, for the caller to escalate) and
    any unavailable player currently sitting there is benched: an empty
    slot scores the same zero as an OUT/bye player, without submitting a
    knowingly-invalid lineup.

    Locked players (candidate.locked — their game already kicked off)
    are pinned to their current slot before any of this runs and never
    reconsidered: ESPN won't let their slot change regardless of what
    the optimizer would otherwise prefer.
    """
    working = {c.entry.player_id: c for c in candidates}
    remaining_ids = set(working)

    assignments: dict[int, int] = {}
    notes: list[str] = []
    unfilled: dict[int, int] = {}

    # Locked players (their game already kicked off) can't be moved at
    # all — pin them to whatever slot they're already in and remove them
    # from consideration entirely, including reducing however many of
    # that slot are still needed, so the fill loop below doesn't try to
    # double-fill a slot a locked player already validly occupies.
    effective_slot_counts = dict(slot_counts)
    for pid in list(remaining_ids):
        candidate = working[pid]
        if not candidate.locked:
            continue

        current_slot = candidate.entry.lineup_slot_id
        assignments[pid] = current_slot
        remaining_ids.discard(pid)

        if current_slot in effective_slot_counts:
            effective_slot_counts[current_slot] = max(
                0, effective_slot_counts[current_slot] - 1
            )

    fill_order = [
        s for s in DEFAULT_FILL_ORDER if s in effective_slot_counts
    ]
    fill_order += [
        s
        for s in effective_slot_counts
        if s not in fill_order and s not in (BENCH_SLOT_ID, IR_SLOT_ID)
    ]

    for slot_id in fill_order:
        count = effective_slot_counts.get(slot_id, 0)
        if count <= 0:
            continue

        eligible_all = [
            working[pid]
            for pid in remaining_ids
            if slot_id in working[pid].entry.eligible_slot_ids
        ]

        available = sorted(
            (c for c in eligible_all if c.available),
            key=lambda c: c.projected_points,
            reverse=True,
        )

        chosen = available[:count]

        for c in chosen:
            assignments[c.entry.player_id] = slot_id
            remaining_ids.discard(c.entry.player_id)

        if len(chosen) < count:
            unfilled[slot_id] = count - len(chosen)
            slot_name = LINEUP_SLOT_IDS.get(slot_id, slot_id)
            if eligible_all:
                blocked = ", ".join(
                    f"{c.entry.name} ({c.availability.reason})"
                    for c in eligible_all
                    if not c.available
                )
                notes.append(
                    f"No startable player for {slot_name} "
                    f"({len(chosen)}/{count} filled) -- every eligible "
                    f"rostered player is unavailable: {blocked}"
                )
            else:
                notes.append(
                    f"No eligible player at all for {slot_name} "
                    f"({len(chosen)}/{count} filled)"
                )

    bench_player_ids: list[int] = []
    for pid in remaining_ids:
        candidate = working[pid]
        if candidate.entry.lineup_slot_id == IR_SLOT_ID:
            continue  # leave IR assignments untouched entirely
        assignments[pid] = BENCH_SLOT_ID
        bench_player_ids.append(pid)

    moves = moves_from_assignments(assignments, working)

    return LineupPlan(
        moves=moves,
        assignments=assignments,
        bench_player_ids=bench_player_ids,
        unfilled_slots=unfilled,
        candidates=working,
        notes=notes,
    )


def moves_from_assignments(
    assignments: dict[int, int],
    candidates: dict[int, LineupCandidate],
) -> list[LineupMove]:
    """
    The ESPN moves needed to go from each player's *current* slot (as
    recorded on the candidate) to his assigned one. Recomputed against
    freshly-read candidates right before submission, so a move is never
    built from a stale "from" slot.
    """
    return [
        LineupMove(
            player_id=pid,
            from_slot_id=candidates[pid].entry.lineup_slot_id,
            to_slot_id=slot_id,
        )
        for pid, slot_id in assignments.items()
        if pid in candidates
        and candidates[pid].entry.lineup_slot_id != slot_id
    ]
