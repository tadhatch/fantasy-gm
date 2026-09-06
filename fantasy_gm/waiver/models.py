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
    action: str  # "add_drop" | "no_move"
    add_player_id: int | None = None
    add_player_name: str | None = None
    drop_player_id: int | None = None
    drop_player_name: str | None = None
    use_waiver: bool = False
    faab_bid: int | None = None
    confidence: float = 0.0
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
