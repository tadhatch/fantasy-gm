# fantasy_gm/trade/models.py

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TradeChip:
    espn_id: int
    name: str
    reason: str


@dataclass(slots=True)
class RosterAnalysis:
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    positional_needs: list[str] = field(default_factory=list)
    trade_chips: list[TradeChip] = field(default_factory=list)
    summary: str = ""


@dataclass(slots=True)
class PartnerCandidate:
    team_id: int
    team_name: str
    fit_score: float
    their_surplus_positions: list[str]
    positions_they_need_from_us: list[str]


@dataclass(slots=True)
class CandidateTrade:
    partner_team_id: int
    partner_team_name: str
    offer_espn_ids: list[int]
    offer_names: list[str]
    request_espn_ids: list[int]
    request_names: list[str]
    reasoning: str
    priority: int = 1


@dataclass(slots=True)
class DeepDiveRequest:
    espn_id: int
    name: str
    reason: str


@dataclass(slots=True)
class GMDecision:
    action: str  # "propose_trade" | "no_move"
    partner_team_id: int | None = None
    partner_team_name: str | None = None
    offer_player_ids: list[int] = field(default_factory=list)
    offer_player_names: list[str] = field(default_factory=list)
    request_player_ids: list[int] = field(default_factory=list)
    request_player_names: list[str] = field(default_factory=list)
    message: str = ""
    confidence: float = 0.0
    reasoning: str = ""


@dataclass(slots=True)
class TradeRunResult:
    roster_analysis: RosterAnalysis
    partner_candidates: list[PartnerCandidate]
    candidate_trades: list[CandidateTrade]
    deep_dives_used: list[DeepDiveRequest]
    decision: GMDecision
    calls_used: int
    executed: bool
