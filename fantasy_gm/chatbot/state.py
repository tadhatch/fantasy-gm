from __future__ import annotations

import logging
import time

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.draft import load_draft_picks
from fantasy_gm.espn.league import load_league_summary
from fantasy_gm.espn.players import load_players

from .models import ChatbotDraftState, DraftPickContext, PlayerNewsItem


logger = logging.getLogger(__name__)


class DraftStateProvider:
    """
    Independent public-state reader for the chatbot.

    This intentionally does NOT read DraftRunner internals or strategy data.
    That prevents the public chatbot from ever seeing candidate rankings,
    queue contents, wait-cost calculations, or upcoming targets.
    """

    def __init__(
        self,
        client: ESPNClient,
        *,
        team_id: int,
        refresh_seconds: float = 15.0,
        recent_picks: int = 12,
    ):
        self.client = client
        self.team_id = team_id
        self.refresh_seconds = max(2.0, refresh_seconds)
        self.recent_pick_limit = max(1, recent_picks)

        self._last_refresh = 0.0
        self._state = ChatbotDraftState(our_team_id=team_id)
        self._player_names: dict[int, str] = {}

    def _load_player_names(self) -> None:
        if self._player_names:
            return
        try:
            players = load_players(self.client, limit=2000)
            self._player_names = {player.id: player.name for player in players}
        except Exception:
            logger.exception("chat: unable to preload ESPN player names")

    def refresh(self, *, force: bool = False) -> ChatbotDraftState:
        now = time.monotonic()
        if not force and now - self._last_refresh < self.refresh_seconds:
            return self._state

        self._load_player_names()

        try:
            summary, _ = load_league_summary(self.client)
            detail, picks = load_draft_picks(self.client)
        except Exception:
            logger.exception("chat: draft state refresh failed")
            return self._state

        made = [pick for pick in picks if pick.player_id > 0]
        last_overall = max((pick.overall_pick for pick in made), default=0)

        current_overall = None
        current_round = None
        current_team_id = None
        for pick in sorted(picks, key=lambda item: item.overall_pick):
            if pick.player_id <= 0:
                current_overall = pick.overall_pick
                current_round = pick.round_id
                current_team_id = pick.team_id
                break

        our_roster = [
            self._player_names.get(pick.player_id, f"ESPN player {pick.player_id}")
            for pick in made
            if pick.team_id == self.team_id
        ]

        recent = []
        for pick in made[-self.recent_pick_limit :]:
            recent.append(
                DraftPickContext(
                    overall_pick=pick.overall_pick,
                    round_number=pick.round_id,
                    pick_in_round=pick.round_pick,
                    team_id=pick.team_id,
                    player_id=pick.player_id,
                    player_name=self._player_names.get(
                        pick.player_id,
                        f"ESPN player {pick.player_id}",
                    ),
                )
            )

        self._state = ChatbotDraftState(
            our_team_id=self.team_id,
            our_team_name=(summary.team_name or "the reservists").lower(),
            current_overall_pick=current_overall,
            current_round=current_round,
            current_team_id=current_team_id,
            our_roster=our_roster,
            recent_picks=recent,
            notable_player_news=self._load_notable_player_news(),
        )
        self._last_refresh = now
        return self._state

    def _load_notable_player_news(self) -> list[PlayerNewsItem]:
        try:
            from fantasy_gm.context.postgres_store import (
                PostgresContextStore,
            )

            notable = PostgresContextStore().notable_recent()
        except Exception:
            logger.exception("chat: unable to load notable player news")
            return []

        return [
            PlayerNewsItem(
                player_name=result.player_name,
                summary=result.summary,
                direct_delta=result.direct_delta,
                category=result.category,
            )
            for result in notable
        ]
