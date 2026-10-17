# fantasy_gm/repair/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fantasy_gm.lineup.availability import Availability
from fantasy_gm.lineup.models import LineupCandidate

# Lineup slot -> the default ESPN position ids that naturally fill it.
# FLEX-type slots map to every position eligible for them.
SLOT_POSITIONS = {
    0: {1},  # QB
    2: {2},  # RB
    4: {3},  # WR
    6: {4},  # TE
    16: {16},  # D/ST
    17: {5},  # K
    23: {2, 3, 4},  # FLEX
}


@dataclass(slots=True)
class RosterAsset:
    """A rostered player as the repair engine sees him."""

    candidate: LineupCandidate
    # Rest-of-season points ESTIMATE (see valuation.projections.
    # extract_ros_projection) -- never a sourced statistic.
    ros_value: float
    # ESPN playerPoolEntry.rosterLocked: can't be dropped right now.
    roster_locked: bool = False
    # ESPN player.droppable (False for the league's undroppable list).
    droppable: bool = True

    @property
    def player_id(self) -> int:
        return self.candidate.entry.player_id

    @property
    def name(self) -> str:
        return self.candidate.entry.name

    @property
    def position_id(self) -> int | None:
        return self.candidate.entry.default_position_id


@dataclass(slots=True)
class FreeAgent:
    player_id: int
    name: str
    position_id: int | None
    eligible_slot_ids: list[int]
    pro_team_id: int | None
    # "FREEAGENT" (immediate) or "WAIVERS" (claim processes later).
    pool_status: str
    availability: Availability
    weekly_projection: float
    ros_value: float
    kickoff: datetime | None
    waiver_clears_at: datetime | None = None

    @property
    def on_waivers(self) -> bool:
        return self.pool_status == "WAIVERS"


@dataclass(slots=True)
class LeagueRosterRules:
    slot_counts: dict[int, int]
    # ESPN position id -> max rostered at that position (absent = no cap).
    position_limits: dict[int, int]
    # Max non-IR roster size: sum of non-IR lineup slot counts.
    roster_max: int
    ir_eligible_statuses: set[str]
    # Why no acquisition is allowed right now (season/matchup limit
    # reached), or None if acquisitions are allowed.
    acquisition_blocked: str | None = None


@dataclass(slots=True)
class DropOption:
    player_id: int
    name: str
    position_id: int | None
    ros_value: float
    # ros_value minus the best free agent's ROS at his position: how much
    # value we'd actually lose, given what's freely replaceable.
    keep_value: float
    rejected_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "position_id": self.position_id,
            "ros_value_estimate": round(self.ros_value, 1),
            "keep_value_estimate": round(self.keep_value, 1),
            "rejected_reason": self.rejected_reason,
        }


@dataclass(slots=True)
class RepairAction:
    slot_id: int
    add: FreeAgent
    drop: DropOption | None
    # Injured player moved to an open IR slot instead of dropping anyone.
    ir_player_id: int | None
    # Estimated roster value change: add ROS + this week's fill - drop ROS.
    value_delta: float
    alternatives: list[dict] = field(default_factory=list)
    drop_options: list[dict] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "add": {
                "player_id": self.add.player_id,
                "name": self.add.name,
                "pool_status": self.add.pool_status,
                "weekly_projection": round(self.add.weekly_projection, 1),
                "ros_value_estimate": round(self.add.ros_value, 1),
                "availability": self.add.availability.as_dict(),
                "waiver_clears_at": (
                    self.add.waiver_clears_at.isoformat()
                    if self.add.waiver_clears_at
                    else None
                ),
            },
            "drop": self.drop.as_dict() if self.drop else None,
            "ir_player_id": self.ir_player_id,
            "value_delta_estimate": round(self.value_delta, 1),
            "alternatives": self.alternatives,
            "drop_options": self.drop_options,
            "reason": self.reason,
        }


@dataclass(slots=True)
class UnrepairableSlot:
    slot_id: int
    reason: str
    details: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class RepairPlan:
    uncovered_slots: dict[int, int]
    actions: list[RepairAction] = field(default_factory=list)
    unrepairable: list[UnrepairableSlot] = field(default_factory=list)
    # Slots already covered by a pending waiver claim we submitted.
    awaiting: dict[int, int] = field(default_factory=dict)
