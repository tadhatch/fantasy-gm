from __future__ import annotations

from dataclasses import dataclass

from fantasy_gm.models.player import Player


@dataclass(frozen=True)
class RankedPlayer:
    player: Player
    score: float


def baseline_rank(players: list[Player]) -> list[RankedPlayer]:
    """Temporary baseline. Replaced next by league-specific projections/ADP/value model."""
    ranked = []
    for player in players:
        score = player.percent_owned or 0.0
        ranked.append(RankedPlayer(player=player, score=score))
    return sorted(ranked, key=lambda x: x.score, reverse=True)
