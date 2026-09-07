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


@dataclass(slots=True)
class LineupPlan:
    moves: list[LineupMove]
    assignments: dict[int, int]
    bench_player_ids: list[int]
    unfilled_slots: dict[int, int]
    notes: list[str] = field(default_factory=list)
