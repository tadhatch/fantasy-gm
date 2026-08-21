from __future__ import annotations

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.models.pick import DraftPick


def load_draft_picks(client: ESPNClient) -> tuple[dict, list[DraftPick]]:
    data = client.get_league(["mDraftDetail", "mTeam"])
    detail = data.get("draftDetail", {})
    picks = [
        DraftPick(
            overall_pick=p["overallPickNumber"],
            round_id=p["roundId"],
            round_pick=p["roundPickNumber"],
            team_id=p["teamId"],
            player_id=p.get("playerId", -1),
        )
        for p in detail.get("picks", [])
    ]
    return detail, picks
