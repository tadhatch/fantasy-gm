from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.models.league import LeagueSummary


def load_league_summary(client: ESPNClient) -> tuple[LeagueSummary, dict]:
    data = client.get_league(["mSettings", "mTeam", "mRoster", "mDraftDetail", "mStatus"])
    settings = data.get("settings", {})
    draft_settings = settings.get("draftSettings", {})
    teams = data.get("teams", [])
    wanted_id = client.settings.espn_team_id
    my_team = next((t for t in teams if t.get("id") == wanted_id), None)
    if not my_team:
        raise RuntimeError(f"Team {wanted_id} not found in league")

    summary = LeagueSummary(
        id=data["id"],
        name=settings.get("name", "Unknown"),
        season=data.get("seasonId", client.settings.espn_season),
        team_count=len(teams),
        team_id=wanted_id,
        team_name=my_team.get("name") or my_team.get("abbrev") or f"Team {wanted_id}",
        draft_type=draft_settings.get("type"),
        draft_date_ms=draft_settings.get("date"),
        pick_order=draft_settings.get("pickOrder", []),
        time_per_selection=draft_settings.get("timePerSelection"),
    )
    return summary, data


def format_draft_time(ms: int | None, tz: str = "America/New_York") -> str:
    if not ms:
        return "unknown"
    dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
    return dt.strftime("%a %b %d, %Y %I:%M %p %Z")
