from __future__ import annotations

import json
from typing import Any, Iterable

import requests

from fantasy_gm.config import Settings


class ESPNError(RuntimeError):
    pass


class ESPNAuthError(ESPNError):
    pass


class ESPNClient:
    def __init__(
        self,
        settings: Settings,
        *,
        league_id: int | None = None,
    ):
        self.settings = settings
        self.league_id = (
            league_id
            if league_id is not None
            else settings.espn_league_id
        )
        self.session = requests.Session()
        self.session.cookies.update({
            "SWID": settings.espn_swid,
            "espn_s2": settings.espn_s2,
        })
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "fantasy-gm/0.1",
        })

    @property
    def league_url(self) -> str:
        return (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{self.settings.espn_season}/segments/0/leagues/"
            f"{self.league_id}"
        )

    def _get(self, url: str, *, params=None, headers=None) -> Any:
        response = self.session.get(url, params=params, headers=headers, timeout=20)
        if response.status_code in (401, 403):
            raise ESPNAuthError(f"ESPN authentication failed ({response.status_code})")
        response.raise_for_status()
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise ESPNError("ESPN returned a non-JSON response; session may be expired") from exc

    def get_league(self, views: Iterable[str]) -> dict[str, Any]:
        params = [("view", view) for view in views]
        return self._get(self.league_url, params=params)

    def get_players(self, limit: int = 2000) -> list[dict[str, Any]]:
        url = (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{self.settings.espn_season}/players"
        )
        fantasy_filter = {
            "players": {
                "limit": limit,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }
        headers = {"X-Fantasy-Filter": json.dumps(fantasy_filter)}
        return self._get(
            url,
            params={"scoringPeriodId": 0, "view": "players_wl"},
            headers=headers,
        )

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


    def get_draft_security(
        self,
        *,
        team_id: int | None = None,
        league_id: int | None = None,
    ) -> str:
        target_league = league_id or self.league_id
        target_team = team_id or self.settings.espn_team_id

        url = (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{self.settings.espn_season}/segments/0/"
            f"leagues/{target_league}/teams/{target_team}/draftSecurity"
        )

        response = self.session.get(url, timeout=20)

        if response.status_code in (401, 403):
            raise ESPNAuthError(
                f"ESPN draftSecurity authentication failed "
                f"({response.status_code})"
            )

        response.raise_for_status()

        # In the captured ESPN browser flow this endpoint returns a plain numeric
        # token, not a JSON object.
        return response.text.strip()