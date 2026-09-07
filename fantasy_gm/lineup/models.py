# fantasy_gm/lineup/models.py

from __future__ import annotations

from dataclasses import dataclass, field

from fantasy_gm.espn.transactions import LineupMove
from fantasy_gm.models.roster import RosterEntry


@dataclass(slots=True)
class LineupCandidate:
    entry: RosterEntry
    projected_points: float
    available: bool
    unavailable_reason: str | None = None
    # Their NFL team's game has already kicked off — ESPN won't let this
    # player's slot change regardless of what the optimizer would
    # otherwise prefer, so they're pinned to their current slot rather
    # than considered for reassignment at all.
    locked: bool = False
    # The two components projected_points is actually made of, kept
    # separately so a consumer can show *why* a decision was made, not
    # just what it was — raw_projection is ESPN's weekly number
    # (pre-injury-discount), evaluation_delta is whatever the cached
    # real-world evaluation contributed on top.
    raw_projection: float | None = None
    evaluation_delta: float = 0.0


@dataclass(slots=True)
class LineupPlan:
    moves: list[LineupMove]
    assignments: dict[int, int]
    bench_player_ids: list[int]
    unfilled_slots: dict[int, int]
    candidates: dict[int, LineupCandidate] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
