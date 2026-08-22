from __future__ import annotations

from fantasy_gm.board.models import BoardPlayer
from fantasy_gm.espn.constants import DEFENSE_IDS
from fantasy_gm.valuation.defense import value_defense

from .models import DefenseStats


def build_defense_board_players(
    defense_stats: list[DefenseStats],
) -> list[BoardPlayer]:
    defense_id_by_team = {
        abs(espn_id) - 16000: espn_id
        for espn_id in DEFENSE_IDS
    }

    result: list[BoardPlayer] = []

    for stats in defense_stats:
        espn_id = defense_id_by_team.get(stats.team_id)
        if espn_id is None:
            continue

        valuation = value_defense(stats)

        # Keep D/ST on a late-round board scale. The strategy layer supplies
        # starter need and -30 early-round suppression.
        score = max(
            6.0,
            min(24.0, valuation.board_score),
        )

        result.append(
            BoardPlayer(
                espn_id=espn_id,
                name=DEFENSE_IDS[espn_id],
                position="DST",
                nfl_team_id=stats.team_id,
                injury_status=None,

                projected_points=valuation.projected_points,
                replacement_points=0.0,
                vor=0.0,

                ecr=None,
                ecr_sd=None,
                adp=None,
                percent_owned=None,

                snap_share=None,
                target_share=None,
                rush_share=None,
                depth_rank=None,

                next_pick_survival=None,
                valuation=None,
                market_score=0.0,
                depth_score=0.0,

                fandom_bonus=0.0,
                fandom_relationship=None,

                board_score=score,
                pick_value=score,
                confidence=valuation.confidence,
                flags=["DST", *valuation.flags],
            )
        )

    result.sort(
        key=lambda p: p.board_score,
        reverse=True,
    )
    return result
