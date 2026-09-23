# fantasy_gm/waiver/models.py

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ExpendablePlayer:
    espn_id: int
    name: str
    reason: str


@dataclass(slots=True)
class RosterAnalysis:
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    positional_needs: list[str] = field(default_factory=list)
    expendable_players: list[ExpendablePlayer] = field(default_factory=list)
    summary: str = ""


@dataclass(slots=True)
class FreeAgentShortlistEntry:
    espn_id: int
    name: str
    position: str
    reason: str


@dataclass(slots=True)
class DeepDiveRequest:
    espn_id: int
    name: str
    reason: str


@dataclass(slots=True)
class CandidateMove:
    add_espn_id: int
    add_name: str
    drop_espn_id: int | None
    drop_name: str | None
    reasoning: str
    priority: int = 1


@dataclass(slots=True)
class GMDecision:
    # NO_MOVE is the default outcome -- a transaction requires
    # affirmative evidence, not just an evaluation run having happened.
    action: str = "no_move"  # "add_drop" | "no_move"
    add_player_id: int | None = None
    add_player_name: str | None = None
    drop_player_id: int | None = None
    drop_player_name: str | None = None
    use_waiver: bool = False
    faab_bid: int | None = None
    confidence: float = 0.0
    # The GM-decision stage's own claim that this is a genuine roster
    # improvement, not just a marginal projection edge. Necessary but
    # not sufficient to submit -- see _gate_transaction() in
    # pipeline.py, the deterministic backstop that actually decides.
    material_upgrade: bool = False
    current_week_delta: float = 0.0
    next_4_weeks_delta: float = 0.0
    # Net rest-of-season roster value change: the add's ongoing value
    # minus the real opportunity cost of losing the drop -- i.e.
    # value_of_roster_after minus value_of_roster_before, not a
    # one-week, one-player projection gap.
    ros_value_delta: float = 0.0
    # True only for a specific, real short-term situation (a bye, an
    # injury, an empty starting slot, a genuinely bad matchup) that
    # justifies weighing short-term value more heavily than the usual
    # rest-of-season bar -- not a routine "this projects a bit higher."
    addresses_roster_need: bool = False
    reasoning: str = ""


@dataclass(slots=True)
class WaiverRunResult:
    roster_analysis: RosterAnalysis
    shortlist: list[FreeAgentShortlistEntry]
    candidate_moves: list[CandidateMove]
    deep_dives_used: list[DeepDiveRequest]
    decision: GMDecision
    calls_used: int
    executed: bool
