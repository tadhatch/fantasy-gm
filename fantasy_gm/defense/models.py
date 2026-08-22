from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(slots=True)
class DefenseStats:
    team_id: int
    team: str
    sacks_per_game: float = 0.0
    pressure_rate: float = 0.0
    takeaways_per_game: float = 0.0
    defensive_tds_per_game: float = 0.0
    points_allowed_per_game: float = 0.0
    yards_allowed_per_game: float = 0.0
    red_zone_td_rate: float | None = None
    explosive_play_rate: float | None = None
    early_schedule_score: float = 0.0
    season_schedule_score: float = 0.0

@dataclass(slots=True)
class DefenseContextEffect:
    source: str
    category: str
    delta: float
    confidence: float
    reason: str
    player_id: int | None = None
    player_name: str | None = None

@dataclass(slots=True)
class DefenseValuation:
    team_id: int
    team: str
    football_score: float
    schedule_score: float
    context_score: float
    projected_points: float
    board_score: float
    confidence: float
    effects: list[DefenseContextEffect] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
