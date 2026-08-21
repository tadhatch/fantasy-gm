from __future__ import annotations

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.models.player import Player


def load_players(client: ESPNClient, limit: int = 2000) -> list[Player]:
    raw = client.get_players(limit=limit)
    players: list[Player] = []
    for item in raw:
        ownership = item.get("ownership") or {}
        players.append(Player(
            id=item["id"],
            name=item.get("fullName", "Unknown"),
            default_position_id=item.get("defaultPositionId"),
            pro_team_id=item.get("proTeamId"),
            percent_owned=ownership.get("percentOwned"),
            percent_started=ownership.get("percentStarted"),
            injury_status=item.get("injuryStatus"),
        ))
    return players
