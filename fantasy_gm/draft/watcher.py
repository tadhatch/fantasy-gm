from __future__ import annotations

import time

from rich.console import Console

from fantasy_gm.config import Settings
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.draft import load_draft_picks

console = Console()


def watch_draft(settings: Settings) -> None:
    client = ESPNClient(settings)
    known: dict[int, int] = {}
    console.print(f"Watching ESPN draft for team {settings.espn_team_id}...", style="bold")

    while True:
        detail, picks = load_draft_picks(client)
        for pick in picks:
            previous = known.get(pick.overall_pick, -1)
            if pick.player_id > 0 and pick.player_id != previous:
                marker = "[bold green]OUR PICK[/]" if pick.team_id == settings.espn_team_id else "pick"
                console.print(
                    f"#{pick.overall_pick:>3} R{pick.round_id}.{pick.round_pick:02d} "
                    f"team={pick.team_id} player_id={pick.player_id} {marker}"
                )
            known[pick.overall_pick] = pick.player_id

        if detail.get("drafted"):
            console.print("Draft complete.", style="bold green")
            return

        time.sleep(settings.espn_poll_seconds)
