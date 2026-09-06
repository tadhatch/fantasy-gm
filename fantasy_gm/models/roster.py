from pydantic import BaseModel


class RosterEntry(BaseModel):
    player_id: int
    name: str
    lineup_slot_id: int
    eligible_slot_ids: list[int] = []
    default_position_id: int | None = None
    injury_status: str | None = None
    acquisition_type: str | None = None


class TeamRoster(BaseModel):
    team_id: int
    team_name: str
    entries: list[RosterEntry]
