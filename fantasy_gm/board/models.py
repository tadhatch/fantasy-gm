from __future__ import annotations

from dataclasses import dataclass, field

from fantasy_gm.valuation.models import PlayerValuation


@dataclass(slots=True)
class BoardPlayer:
    espn_id: int
    name: str
    position: str
    projected_points: float
    replacement_points: float
    vor: float

    nfl_team_id: int | None
    injury_status: str | None
    fandom_relationship: str | None = None
    ecr: float | None = None
    ecr_sd: float | None = None
    adp: float | None = None
    percent_owned: float | None = None
    snap_share: float | None = None
    target_share: float | None = None
    rush_share: float | None = None
    depth_rank: int | None = None
    next_pick_survival: float | None = None
    valuation: PlayerValuation | None = None

    fandom_bonus: float = 0.0
    market_score: float = 0.0
    depth_score: float = 0.0
    board_score: float = 0.0
    pick_value: float = 0.0
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)
