import os

import requests
from dotenv import load_dotenv

load_dotenv()

league_id = os.environ["ESPN_LEAGUE_ID"]
season = os.environ.get("ESPN_SEASON", "2026")

url = (
    f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
    f"seasons/{season}/segments/0/leagues/{league_id}"
)

cookies = {
    "SWID": os.environ["ESPN_SWID"],
    "espn_s2": os.environ["ESPN_S2"],
}

params = [
    ("view", "mSettings"),
    ("view", "mTeam"),
    ("view", "mRoster"),
    ("view", "mDraftDetail"),
    ("view", "mStatus"),
]

response = requests.get(
    url,
    params=params,
    cookies=cookies,
    timeout=20,
)

response.raise_for_status()

data = response.json()

print("League:", data["settings"]["name"])
print("Teams:", len(data.get("teams", [])))
print("Season:", data.get("seasonId"))

print("Top-level keys:", sorted(data.keys()))

for team in data.get("teams", []):
    print(
        f"Team {team['id']}: "
        f"{team.get('name', team.get('abbrev', 'Unknown'))}"
    )

import json
from datetime import datetime, timezone

settings = data.get("settings", {})
draft = data.get("draftDetail", {})
status = data.get("status", {})

print("\n=== DRAFT ===")
print(json.dumps(draft, indent=2))

print("\n=== DRAFT SETTINGS ===")
print(json.dumps(settings.get("draftSettings", {}), indent=2))

print("\n=== ROSTER SETTINGS ===")
print(json.dumps(settings.get("rosterSettings", {}), indent=2))

print("\n=== SCORING SETTINGS ===")
print(json.dumps(settings.get("scoringSettings", {}), indent=2))

print("\n=== STATUS ===")
print(json.dumps(status, indent=2))
