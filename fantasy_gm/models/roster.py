from datetime import datetime

from pydantic import BaseModel


class RosterEntry(BaseModel):
    player_id: int
    name: str
    lineup_slot_id: int
    eligible_slot_ids: list[int] = []
    default_position_id: int | None = None
    pro_team_id: int | None = None
    injury_status: str | None = None
    acquisition_type: str | None = None
    # ESPN's own per-player lineup lock (playerPoolEntry.lineupLocked) --
    # authoritative when present; None when the payload didn't carry it.
    lineup_locked: bool | None = None
    # player.active: whether ESPN considers this player on an active NFL
    # roster at all. False means he can't score this week regardless of
    # injury designation.
    nfl_active: bool | None = None
    # ESPN playerPoolEntry.rosterLocked: can't be dropped right now.
    roster_locked: bool | None = None
    # ESPN player.droppable: False for the league's undroppable list.
    droppable: bool | None = None


class TeamRoster(BaseModel):
    team_id: int
    team_name: str
    entries: list[RosterEntry]
    # When this roster was read from ESPN -- availability decisions use
    # it to refuse to treat stale data as confirmation of anything.
    fetched_at: datetime | None = None
