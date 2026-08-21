from pydantic import BaseModel


class DraftPick(BaseModel):
    overall_pick: int
    round_id: int
    round_pick: int
    team_id: int
    player_id: int

    @property
    def is_made(self) -> bool:
        return self.player_id > 0
