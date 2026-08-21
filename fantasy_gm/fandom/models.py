from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(slots=True)
class FandomAffinity:
    espn_id: int
    player_name: str
    favorite_team: str
    current_player: bool = False
    seasons_with_team: int = 0
    first_season: int | None = None
    last_season: int | None = None
    significance: float = 0.0
    bonus: float = 0.0
    relationship: str = "none"
    notes: list[str] = field(default_factory=list)
