from __future__ import annotations

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient

from .protocol import (
    AutoDraft,
    AutoSuggest,
    Clock,
    Joined,
    Selected,
    Selecting,
    State,
    Unknown,
)
from .session import DraftSession


def watch_live_draft(
    client: ESPNClient,
    *,
    team_id: int,
    show_clock: bool = False,
    show_unknown: bool = False,
) -> None:
    console = Console()

    selected_count = 0
    current_team: int | None = None

    console.print(
        f"[bold]Connecting to ESPN live draft[/bold] "
        f"league={client.league_id} team={team_id}"
    )

    with DraftSession(client, team_id=team_id) as session:
        console.print("[green]Connected to ESPN draft WebSocket.[/green]")

        for event in session.events():
            if isinstance(event, Joined):
                console.print(
                    f"Joined draft room as team {event.team_id}"
                )

            elif isinstance(event, State):
                console.print(f"Draft state: {event.state}")

            elif isinstance(event, Selecting):
                current_team = event.team_id
                ours = event.team_id == team_id

                if ours:
                    console.print(
                        f"\n[bold green]OUR PICK[/bold green] "
                        f"— {event.milliseconds / 1000:.0f}s on clock"
                    )
                else:
                    console.print(
                        f"Team {event.team_id} selecting "
                        f"— {event.milliseconds / 1000:.0f}s"
                    )

            elif isinstance(event, Selected):
                selected_count += 1
                ours = event.team_id == team_id
                suffix = (
                    " [bold green]OUR PICK[/bold green]"
                    if ours else ""
                )
                console.print(
                    f"Pick {selected_count:>3}: "
                    f"Team {event.team_id:>2} → "
                    f"ESPN player {event.player_id}{suffix}"
                )

            elif isinstance(event, AutoSuggest):
                if current_team == team_id:
                    console.print(
                        f"ESPN autosuggest: player {event.player_id}"
                    )

            elif isinstance(event, AutoDraft):
                if event.team_id == team_id:
                    console.print(
                        f"ESPN autodraft: "
                        f"{'ON' if event.enabled else 'OFF'}"
                    )

            elif isinstance(event, Clock) and show_clock:
                console.print(
                    f"[dim]clock state={event.state} "
                    f"ms={event.milliseconds} team={event.team_id}[/dim]"
                )

            elif isinstance(event, Unknown) and show_unknown:
                command = event.command or "EMPTY"
                console.print(f"[dim]{command}: {event.raw[:120]!r}[/dim]")
