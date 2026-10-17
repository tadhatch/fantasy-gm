# fantasy_gm/lineup/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fantasy_gm.espn.transactions import LineupMove
from fantasy_gm.models.roster import RosterEntry

from .availability import Availability


@dataclass(slots=True)
class LineupCandidate:
    entry: RosterEntry
    projected_points: float
    # Hard eligibility -- see availability.evaluate_availability(). A
    # high projection never overrides this.
    availability: Availability
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
    kickoff: datetime | None = None

    @property
    def available(self) -> bool:
        return self.availability.can_start

    @property
    def unavailable_reason(self) -> str | None:
        if self.availability.can_start and not (
            self.availability.requires_revalidation
        ):
            return None
        return self.availability.reason


@dataclass(slots=True)
class LineupPlan:
    moves: list[LineupMove]
    assignments: dict[int, int]
    bench_player_ids: list[int]
    unfilled_slots: dict[int, int]
    candidates: dict[int, LineupCandidate] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
