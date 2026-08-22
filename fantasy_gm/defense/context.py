from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
from fantasy_gm.context.models import AIContextResult
from .models import DefenseContextEffect

ROLE_MULTIPLIER = {
    "EDGE": 1.00, "DE": 1.00, "OLB": 0.90, "DT": 0.75, "NT": 0.65,
    "MLB": 0.85, "ILB": 0.80, "LB": 0.80, "CB": 0.90, "S": 0.80, "DB": 0.75,
}

@dataclass(slots=True)
class DefenderIdentity:
    espn_id: int
    name: str
    team_id: int
    position: str
    depth_rank: int | None = None

def _starter_multiplier(depth_rank: int | None) -> float:
    if depth_rank is None: return 0.65
    if depth_rank == 1: return 1.00
    if depth_rank == 2: return 0.70
    return 0.45

def propagate_player_context_to_defense(
    defender: DefenderIdentity,
    context: AIContextResult,
) -> DefenseContextEffect | None:
    role = ROLE_MULTIPLIER.get(defender.position.upper(), 0.65)
    starter = _starter_multiplier(defender.depth_rank)
    direct = max(-10.0, min(10.0, context.direct_delta))
    confidence = max(0.0, min(1.0, context.confidence))

    unit_delta = direct * 0.22 * role * starter * confidence
    availability = max(0.0, min(1.0, context.availability_risk))
    distraction = max(0.0, min(1.0, context.distraction_risk))
    unit_delta -= availability * 1.25 * role * starter
    unit_delta -= distraction * 0.55 * role * starter
    unit_delta = max(-4.0, min(2.5, unit_delta))

    if abs(unit_delta) < 0.15:
        return None

    return DefenseContextEffect(
        source="player_context",
        category=context.category,
        delta=unit_delta,
        confidence=confidence,
        reason=f"{defender.name}: {context.summary}",
        player_id=defender.espn_id,
        player_name=defender.name,
    )

def aggregate_defense_context(
    effects: Iterable[DefenseContextEffect],
    *,
    floor: float = -10.0,
    ceiling: float = 6.0,
) -> tuple[float, list[DefenseContextEffect]]:
    usable = [e for e in effects if abs(e.delta) >= 0.05]
    usable.sort(key=lambda e: abs(e.delta), reverse=True)
    total = 0.0
    decay = 1.0
    for effect in usable:
        total += effect.delta * decay
        decay *= 0.82
    return max(floor, min(ceiling, total)), usable
