from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
import nflreadpy as nfl
from .models import FandomAffinity

class FandomService:
    """Small deterministic favorite-team bias. Close-tie breaker, not football logic."""

    def __init__(self, favorite_team: str, *, current_season: int = 2026,
                 history_start: int = 2002, max_bonus: float = 1.5):
        self.favorite_team = favorite_team.strip().upper()
        self.current_season = current_season
        self.history_start = history_start
        self.max_bonus = max(0.0, float(max_bonus))

    @lru_cache(maxsize=1)
    def build_index(self) -> dict[int, FandomAffinity]:
        rosters = nfl.load_rosters(list(range(self.history_start, self.current_season + 1)))
        history: dict[int, set[int]] = defaultdict(set)
        names: dict[int, str] = {}
        current_team: dict[int, str] = {}

        for row in rosters.to_dicts():
            espn_id = _int(row.get("espn_id"))
            season = _int(row.get("season"))
            team = str(row.get("team") or "").upper()
            if espn_id is None or season is None:
                continue
            if row.get("full_name"):
                names[espn_id] = str(row["full_name"])
            if team == self.favorite_team:
                history[espn_id].add(season)
            if season == self.current_season:
                current_team[espn_id] = team

        result: dict[int, FandomAffinity] = {}
        for espn_id in set(history) | set(current_team):
            years = sorted(history.get(espn_id, set()))
            current = current_team.get(espn_id) == self.favorite_team
            if not current and not years:
                continue
            seasons_with = len(years)
            last = years[-1] if years else None
            significance = self._significance(current, seasons_with, last)
            bonus = self._bonus(current, significance)
            result[espn_id] = FandomAffinity(
                espn_id=espn_id,
                player_name=names.get(espn_id, f"ESPN {espn_id}"),
                favorite_team=self.favorite_team,
                current_player=current,
                seasons_with_team=seasons_with,
                first_season=years[0] if years else None,
                last_season=last,
                significance=significance,
                bonus=bonus,
                relationship="current" if current else "alumni",
                notes=[
                    f"Current {self.favorite_team} player" if current else
                    f"{self.favorite_team} alumni: {years[0]}-{years[-1]} ({seasons_with} seasons)"
                ],
            )
        return result

    def get(self, espn_id: int) -> FandomAffinity | None:
        return self.build_index().get(espn_id)

    def _significance(self, current: bool, seasons_with: int, last_season: int | None) -> float:
        tenure = min(1.0, seasons_with / 8.0)
        recency = 0.0 if last_season is None else max(0.0, 1.0 - (self.current_season - last_season) / 10.0)
        return max(0.0, min(1.0, 0.55 * tenure + 0.25 * recency + 0.20 * (1.0 if current else 0.0)))

    def _bonus(self, current: bool, significance: float) -> float:
        multiplier = (0.60 + 0.40 * significance) if current else (0.55 * significance)
        return round(min(self.max_bonus, self.max_bonus * multiplier), 3)

def _int(value: object) -> int | None:
    try:
        return None if value is None else int(float(value))
    except (TypeError, ValueError):
        return None
