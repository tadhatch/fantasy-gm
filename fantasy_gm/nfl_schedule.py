# fantasy_gm/nfl_schedule.py

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from fantasy_gm.espn.constants import PRO_TEAM_ABBR


logger = logging.getLogger(__name__)

NFL_TZ = ZoneInfo("America/New_York")

# nflverse and ESPN don't always use the same team abbreviation for the
# same franchise — bridge the ones known to differ rather than silently
# failing to match. A miss here (or any other) is handled conservatively
# by the caller: unknown means locked, not unlocked.
ESPN_TO_NFLVERSE_ABBR = {
    "WSH": "WAS",
    "LAR": "LA",
}

_CACHE_TTL_SECONDS = 3 * 3600
_cache: dict[int, tuple[float, list["ScheduledGame"]]] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_snapshot (
    game_id TEXT PRIMARY KEY,
    season INT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    kickoff TIMESTAMPTZ NOT NULL
);
"""


@dataclass(slots=True)
class ScheduledGame:
    game_id: str
    home_team: str
    away_team: str
    kickoff: datetime


def load_season_games(
    season: int, *, force: bool = False
) -> list[ScheduledGame]:
    """
    This season's games with real kickoff times, from nflverse's
    published schedule — best-effort: nflreadpy's exact column
    names/formats haven't been verified against a live run, so a parse
    failure on one row just drops that row rather than the whole
    fetch failing.

    Detects and logs kickoff-time changes against the last snapshot
    stored in Postgres (flex scheduling moves games after the season's
    already published) — this only means something across calls/restarts
    because it's durable, not in-memory.
    """
    cached = _cache.get(season)
    now_ts = time.monotonic()
    if not force and cached and now_ts - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    games: list[ScheduledGame] = []
    try:
        import nflreadpy as nfl

        schedule = nfl.load_schedules([season])
        for row in schedule.to_dicts():
            game_id = row.get("game_id")
            gameday = row.get("gameday")
            gametime = row.get("gametime")
            home = row.get("home_team")
            away = row.get("away_team")
            if not all((game_id, gameday, gametime, home, away)):
                continue
            try:
                kickoff = datetime.strptime(
                    f"{gameday} {gametime}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=NFL_TZ)
            except ValueError:
                continue

            games.append(
                ScheduledGame(
                    game_id=str(game_id),
                    home_team=str(home),
                    away_team=str(away),
                    kickoff=kickoff,
                )
            )
    except Exception:
        logger.exception("nfl_schedule: failed to load season schedule")
        if cached:
            return cached[1]  # stale beats nothing

    if games:
        _detect_and_record_changes(season, games)

    _cache[season] = (now_ts, games)
    return games


def team_kickoff_this_week(
    season: int,
    espn_pro_team_id: int | None,
    *,
    now: datetime,
    force: bool = False,
) -> datetime | None:
    """
    Kickoff time for this team's game closest to `now` (within 4 days
    either way, so a bye week correctly returns None instead of matching
    some other week's game).
    """
    if espn_pro_team_id is None:
        return None

    espn_abbr = PRO_TEAM_ABBR.get(espn_pro_team_id)
    if espn_abbr is None:
        return None

    target = ESPN_TO_NFLVERSE_ABBR.get(espn_abbr, espn_abbr)

    candidates = [
        game.kickoff
        for game in load_season_games(season, force=force)
        if target in (game.home_team, game.away_team)
        and abs((game.kickoff - now).days) <= 4
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda k: abs((k - now).total_seconds()))


def is_team_locked(
    season: int,
    espn_pro_team_id: int | None,
    *,
    now: datetime,
) -> bool:
    """
    True once this team's game this week has kicked off. Unknown team,
    unknown schedule, or a bye week is NOT the same as locked — those
    return False, since callers only need this to prevent moving an
    already-locked player, not to reason about byes (already handled by
    "no weekly projection" elsewhere).
    """
    kickoff = team_kickoff_this_week(season, espn_pro_team_id, now=now)
    if kickoff is None:
        return False
    return now >= kickoff


def is_team_on_bye(
    season: int,
    espn_pro_team_id: int | None,
    *,
    now: datetime,
) -> bool | None:
    """
    True if this team has no game in the current week's window (a real
    bye, confirmed against the schedule), False if it does, None if this
    can't be determined at all (unmapped team or the schedule itself
    failed to load) — callers should fall back to another signal rather
    than assume either way when this is None, since it's genuinely
    unknown, not "not on bye".
    """
    if espn_pro_team_id is None:
        return None

    espn_abbr = PRO_TEAM_ABBR.get(espn_pro_team_id)
    if espn_abbr is None:
        return None

    games = load_season_games(season)
    if not games:
        return None

    target = ESPN_TO_NFLVERSE_ABBR.get(espn_abbr, espn_abbr)

    has_game_this_week = any(
        target in (game.home_team, game.away_team)
        and abs((game.kickoff - now).days) <= 4
        for game in games
    )
    return not has_game_this_week


def _detect_and_record_changes(
    season: int, games: list[ScheduledGame]
) -> None:
    try:
        dsn = os.environ["DATABASE_URL"]
        with psycopg.connect(dsn) as conn:
            conn.execute(SCHEMA)

            rows = conn.execute(
                "SELECT game_id, kickoff FROM schedule_snapshot "
                "WHERE season = %s",
                (season,),
            ).fetchall()
            previous = {game_id: kickoff for game_id, kickoff in rows}

            for game in games:
                old_kickoff = previous.get(game.game_id)
                if old_kickoff is not None and old_kickoff != game.kickoff:
                    logger.warning(
                        "nfl_schedule: kickoff changed for %s @ %s "
                        "(likely flex): %s -> %s",
                        game.away_team,
                        game.home_team,
                        old_kickoff,
                        game.kickoff,
                    )

                conn.execute(
                    """
                    INSERT INTO schedule_snapshot
                        (game_id, season, home_team, away_team, kickoff)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (game_id) DO UPDATE SET
                        home_team = EXCLUDED.home_team,
                        away_team = EXCLUDED.away_team,
                        kickoff = EXCLUDED.kickoff
                    """,
                    (
                        game.game_id,
                        season,
                        game.home_team,
                        game.away_team,
                        game.kickoff,
                    ),
                )

            conn.commit()
    except Exception:
        # Change detection is a nice-to-have, not load-bearing — never
        # block a real schedule fetch on it.
        logger.exception("nfl_schedule: failed to record schedule snapshot")
