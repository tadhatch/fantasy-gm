from pydantic import BaseModel


class LeagueSummary(BaseModel):
    id: int
    name: str
    season: int
    team_count: int
    team_id: int
    team_name: str
    draft_type: str | None = None
    draft_date_ms: int | None = None
    pick_order: list[int] = []
    time_per_selection: int | None = None
