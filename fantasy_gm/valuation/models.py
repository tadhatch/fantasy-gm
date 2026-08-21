from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AdjustmentCategory(str, Enum):
    INJURY = "injury"
    ROLE = "role"
    OPPORTUNITY = "opportunity"
    OFF_FIELD = "off_field"
    CONTRACT = "contract"
    COACHING = "coaching"
    TEAM_ENVIRONMENT = "team_environment"
    OFFENSIVE_LINE = "offensive_line"
    SUSPENSION = "suspension"
    NEWS = "news"


@dataclass(slots=True)
class Evidence:
    source: str
    title: str
    published_at: str | None = None
    url: str | None = None
    summary: str | None = None
    reliability: float = 0.75


@dataclass(slots=True)
class ContextAdjustment:
    player_id: int
    category: AdjustmentCategory
    delta: float
    confidence: float
    reason: str
    evidence: list[Evidence] = field(default_factory=list)
    expires_at: str | None = None

    def bounded_delta(self, cap: float = 20.0) -> float:
        # Prevent a single article/model judgment from overwhelming the board.
        return max(-cap, min(cap, self.delta)) * max(0.0, min(1.0, self.confidence))


@dataclass(slots=True)
class PlayerMetrics:
    espn_id: int
    name: str
    position: str
    nfl_team: str | int | None = None

    projected_points: float = 0.0
    replacement_points: float = 0.0

    snap_share: float | None = None
    route_participation: float | None = None
    target_share: float | None = None
    rush_share: float | None = None
    red_zone_share: float | None = None

    yards_per_route_run: float | None = None
    rush_yards_over_expected: float | None = None
    receiving_epa_per_target: float | None = None

    age: float | None = None
    injury_risk: float = 0.0
    role_volatility: float = 0.0

    adp: float | None = None
    espn_ownership_pct: float | None = None

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlayerValuation:
    player_id: int
    name: str
    position: str

    projection_score: float = 0.0
    replacement_score: float = 0.0
    usage_score: float = 0.0
    efficiency_score: float = 0.0
    risk_score: float = 0.0
    context_score: float = 0.0
    scarcity_score: float = 0.0

    intrinsic_value: float = 0.0
    pick_value: float = 0.0
    confidence: float = 0.0

    adjustments: list[ContextAdjustment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def breakdown(self) -> dict[str, float]:
        return {
            "projection": self.projection_score,
            "replacement": self.replacement_score,
            "usage": self.usage_score,
            "efficiency": self.efficiency_score,
            "risk": self.risk_score,
            "context": self.context_score,
            "scarcity": self.scarcity_score,
            "intrinsic": self.intrinsic_value,
            "pick": self.pick_value,
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class PlayerRelationship:
    source_player_id: int
    target_player_id: int
    relationship: str
    strength: float
    target_position: str | None = None
    notes: str | None = None
