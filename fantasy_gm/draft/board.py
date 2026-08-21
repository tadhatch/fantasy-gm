from __future__ import annotations

from fantasy_gm.models.player import Player


def available_players(players: list[Player], drafted_ids: set[int]) -> list[Player]:
    return [p for p in players if p.id not in drafted_ids]
