from pydantic import BaseModel


class Player(BaseModel):
    id: int
    name: str
    default_position_id: int | None = None
    pro_team_id: int | None = None
    percent_owned: float | None = None
    percent_started: float | None = None
    injury_status: str | None = None
