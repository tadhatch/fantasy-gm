# fantasy_gm/espn/roster.py

from __future__ import annotations

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.models.roster import RosterEntry, TeamRoster


def load_all_rosters(client: ESPNClient) -> dict[int, TeamRoster]:
    """
    Read every team's current roster (starters + bench) via the mRoster view.

    Used both to inspect our own team before making a move and to see what
    other teams are running, which the trade/waiver workers need.
    """
    data = client.get_league(["mRoster", "mTeam"])

    rosters: dict[int, TeamRoster] = {}

    for team in data.get("teams", []):
        team_id = team.get("id")
        if team_id is None:
            continue

        team_name = team.get("name") or team.get("abbrev") or f"Team {team_id}"
        roster = team.get("roster", {})
        entries: list[RosterEntry] = []

        for entry in roster.get("entries", []):
            player_id = entry.get("playerId")
            if player_id is None:
                continue

            pool_entry = entry.get("playerPoolEntry") or {}
            player = pool_entry.get("player") or {}

            entries.append(
                RosterEntry(
                    player_id=player_id,
                    name=player.get("fullName", f"Player {player_id}"),
                    lineup_slot_id=entry.get("lineupSlotId", -1),
                    eligible_slot_ids=player.get("eligibleSlots", []),
                    default_position_id=player.get("defaultPositionId"),
                    injury_status=player.get("injuryStatus")
                    or pool_entry.get("injuryStatus"),
                    acquisition_type=entry.get("acquisitionType"),
                )
            )

        rosters[team_id] = TeamRoster(
            team_id=team_id,
            team_name=team_name,
            entries=entries,
        )

    return rosters


def load_team_roster(client: ESPNClient, team_id: int) -> TeamRoster:
    rosters = load_all_rosters(client)

    roster = rosters.get(team_id)
    if roster is None:
        raise KeyError(f"Team {team_id} not found in league roster data")

    return roster


def current_scoring_period(client: ESPNClient) -> int:
    """
    Best-effort current scoring period lookup.

    ESPN exposes this in a few places depending on view/time of season;
    fall back through the ones observed in practice.
    """
    data = client.get_league(["mStatus"])

    top_level = data.get("scoringPeriodId")
    if isinstance(top_level, int):
        return top_level

    status = data.get("status", {})
    for key in ("currentMatchupPeriod", "latestScoringPeriod"):
        value = status.get(key)
        if isinstance(value, int):
            return value

    raise RuntimeError(
        "Could not determine current scoring period from ESPN league status"
    )
