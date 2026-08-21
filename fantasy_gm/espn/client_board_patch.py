"""
COPY THESE METHODS INTO fantasy_gm/espn/client.py inside ESPNClient.

They use the league-context kona_player_info feed because that response can
include projections calculated using the league's own scoring settings.
"""

def get_player_pool(self, limit: int = 2000):
    fantasy_filter = {
        "players": {
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    headers = {"X-Fantasy-Filter": json.dumps(fantasy_filter)}
    return self._get(
        self.league_url,
        params=[
            ("view", "kona_player_info"),
            ("scoringPeriodId", 0),
        ],
        headers=headers,
    )
