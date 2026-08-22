from fantasy_gm.board.models import BoardPlayer
from fantasy_gm.espn.constants import DEFENSE_IDS


def build_defense_players() -> list[BoardPlayer]:
    players: list[BoardPlayer] = []

    for espn_id, name in DEFENSE_IDS.items():
        players.append(
            BoardPlayer(
                espn_id=espn_id,
                name=name,
                position="DST",
                nfl_team_id=abs(espn_id) - 16000,
                injury_status=None,

                projected_points=0.0,
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

                board_score=0.0,
                pick_value=0.0,
                confidence=1.0,
                flags=["DST"],
            )
        )

    return players