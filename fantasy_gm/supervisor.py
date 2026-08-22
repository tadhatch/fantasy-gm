from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.draft import load_draft_picks
from fantasy_gm.espn.league import load_league_summary


POLL_SECONDS = 60
DRAFT_LEAD_TIME = timedelta(minutes=5)

console = Console()


def run_supervisor(client: ESPNClient) -> None:
    console.print("[bold green]Fantasy GM supervisor started[/bold green]")

    summary, _ = load_league_summary(client)

    draft_time = datetime.fromtimestamp(
        summary.draft_date_ms / 1000,
        tz=timezone.utc,
    )

    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")

    console.print(
        "Draft scheduled for: "
        f"{draft_time.astimezone(eastern):%Y-%m-%d %I:%M %p %Z}"
    )

    draft_process: subprocess.Popen | None = None

    while True:
        try:
            draft_data, _ = load_draft_picks(client)

            drafted = bool(draft_data.get("drafted"))
            in_progress = bool(draft_data.get("inProgress"))

            # Reap the child if it exited.
            if draft_process is not None:
                return_code = draft_process.poll()

                if return_code is not None:
                    console.print(
                        f"[yellow]Draft process exited "
                        f"with code {return_code}[/yellow]"
                    )
                    draft_process = None

            if drafted:
                time.sleep(POLL_SECONDS)
                continue

            now = datetime.now(timezone.utc)
            time_until_draft = draft_time - now

            should_launch = (
                in_progress
                or time_until_draft <= DRAFT_LEAD_TIME
            )

            if should_launch and draft_process is None:
                console.print(
                    "[bold cyan]Launching autonomous draft manager[/bold cyan]"
                )

                draft_process = subprocess.Popen(
                    [
                        "fantasy-gm",
                        "draft",
                        "run",
                        "--auto-select",
                    ]
                )

            time.sleep(POLL_SECONDS)

        except Exception as exc:
            console.print(
                f"[red]Supervisor error: {exc!r}[/red]"
            )
            time.sleep(POLL_SECONDS)