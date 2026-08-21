from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EvidenceItem:
    title: str
    source: str
    url: str | None = None
    published_at: str | None = None
    claim: str | None = None
    reliability: float = 0.7


@dataclass(slots=True)
class RelatedPlayerEffect:
    player_name: str
    relationship: str
    delta: float
    confidence: float
    reason: str


@dataclass(slots=True)
class AIContextResult:
    espn_id: int
    player_name: str
    researched_at: str
    model: str

    summary: str
    direct_delta: float
    confidence: float
    category: str
    availability_risk: float = 0.0
    distraction_risk: float = 0.0
    role_change: float = 0.0
    injury_change: float = 0.0

    evidence: list[EvidenceItem] = field(default_factory=list)
    related_players: list[RelatedPlayerEffect] = field(default_factory=list)
    raw_response_id: str | None = None

    def weighted_delta(self, cap: float = 15.0) -> float:
        delta = max(-cap, min(cap, self.direct_delta))
        confidence = max(0.0, min(1.0, self.confidence))
        return delta * confidence
