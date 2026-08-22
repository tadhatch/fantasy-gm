from __future__ import annotations
import time

from fantasy_gm.espn.draft import load_draft_picks


import typer
from rich.console import Console
from rich.table import Table

from fantasy_gm.config import get_settings
from fantasy_gm.draft.watcher import watch_draft
from fantasy_gm.draft.watcher_live import watch_live_draft
from fantasy_gm.draft.runner import DraftRunner, DraftRunnerConfig
from fantasy_gm.draft.session import DraftSession
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import POSITION_IDS
from fantasy_gm.espn.draft import load_draft_picks
from fantasy_gm.espn.league import format_draft_time, load_league_summary
from fantasy_gm.espn.players import load_players
from fantasy_gm.board.builder import build_board
from fantasy_gm.board.renderer import render_board
from fantasy_gm.context.renderer import render_context
from fantasy_gm.context.service import refresh_context
from fantasy_gm.chatbot.cli_commands import build_chatbot_typer
from fantasy_gm.supervisor import run_supervisor

app = typer.Typer(no_args_is_help=True)
console = Console()
context_app = typer.Typer(help="AI current-context research")
app.add_typer(context_app, name="context")
chatbot_app = build_chatbot_typer()
app.add_typer(chatbot_app, name="chatbot")

@app.command()
def run():
    """Run the always-on Fantasy GM supervisor."""

    settings = get_settings()
    client = ESPNClient(settings)
    run_supervisor(client)

@app.command()
def league() -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    summary, _ = load_league_summary(client)
    _, picks = load_draft_picks(client)
    our_picks = [p.overall_pick for p in picks if p.team_id == summary.team_id]

    console.print(f"[bold]{summary.name}[/bold]")
    console.print(f"Teams: {summary.team_count}")
    console.print(f"Season: {summary.season}")
    console.print(f"Team: {summary.team_name} (ID {summary.team_id})")
    console.print(f"Draft: {summary.draft_type}")
    console.print(f"Draft time: {format_draft_time(summary.draft_date_ms)}")
    console.print(f"Pick timer: {summary.time_per_selection}s")
    console.print("Our picks: " + ", ".join(map(str, our_picks)))


@app.command()
def players(limit: int = typer.Option(100, min=1, max=2000)) -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    rows = load_players(client, limit=max(limit, 250))
    rows.sort(key=lambda p: p.percent_owned or 0.0, reverse=True)

    table = Table(title=f"ESPN Players — top {limit} by ownership")
    table.add_column("ESPN ID", justify="right")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("NFL Team", justify="right")
    table.add_column("Owned %", justify="right")
    table.add_column("Injury")
    for p in rows[:limit]:
        table.add_row(
            str(p.id), p.name, POSITION_IDS.get(p.default_position_id, str(p.default_position_id or "?")),
            str(p.pro_team_id or "-"), f"{p.percent_owned:.1f}" if p.percent_owned is not None else "-",
            p.injury_status or "-",
        )
    console.print(table)


@app.command()
def board(top: int = typer.Option(50, min=1, max=300)) -> None:
    settings = get_settings()
    client = ESPNClient(settings)


    _, draft_picks = load_draft_picks(client)
    selected = [p for p in draft_picks if p.player_id > 0]
    last_overall = max((p.overall_pick for p in selected), default=0)

    our_future = sorted(
        p.overall_pick
        for p in draft_picks
        if p.team_id == settings.espn_team_id and p.overall_pick > last_overall
    )

    if our_future and our_future[0] == last_overall + 1:
        next_pick = our_future[1] if len(our_future) > 1 else None
    else:
        next_pick = our_future[0] if our_future else None

    data = client.get_player_pool(limit=max(500, top * 5))
    pool = data.get("players", []) if isinstance(data, dict) else data

    rows = build_board(
        pool,
        season=settings.espn_season,
        next_pick=next_pick,
        history_season=settings.espn_season - 1,
        favorite_team=settings.favorite_team,
        fandom_weight=settings.fandom_weight,
    )
    render_board(rows, limit=top)


@context_app.command("refresh")
def context_refresh(
    top: int = typer.Option(30, min=1, max=200),
    force: bool = typer.Option(False, "--force"),
    freshness_hours: int = typer.Option(12, min=1, max=168),
    max_deep_dives: int = typer.Option(
        8,
        "--max-deep-dives",
        min=0,
        max=50,
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings)

    _, draft_picks = load_draft_picks(client)
    selected = [p for p in draft_picks if p.player_id > 0]
    last_overall = max((p.overall_pick for p in selected), default=0)

    our_future = sorted(
        p.overall_pick
        for p in draft_picks
        if p.team_id == settings.espn_team_id and p.overall_pick > last_overall
    )
    if our_future and our_future[0] == last_overall + 1:
        next_pick = our_future[1] if len(our_future) > 1 else None
    else:
        next_pick = our_future[0] if our_future else None

    data = client.get_player_pool(limit=max(500, top * 8))
    pool = data.get("players", []) if isinstance(data, dict) else data

    # build_board consumes existing context cache, but that is okay:
    # we're only using its current ordering to choose research candidates.
    board_rows = build_board(
        pool,
        season=settings.espn_season,
        next_pick=next_pick,
        history_season=settings.espn_season - 1,
    )

    console = Console()

    def progress(i, total, player, state, decision):
        console.print(f"[{i:>3}/{total}] {state:>20}  {player.name}")

    refresh_context(
        board_rows,
        top=top,
        force=force,
        freshness_hours=freshness_hours,
        progress=progress,
        next_pick=next_pick,
        max_deep_dives=max_deep_dives,
    )

    console.print("\n[green]Context refresh complete.[/green]")


@context_app.command("show")
def context_show(limit: int = typer.Option(50, min=1, max=300)) -> None:
    render_context(limit=limit)



draft_app = typer.Typer(no_args_is_help=True)
app.add_typer(draft_app, name="draft")


@draft_app.command("status")
def draft_status() -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    detail, picks = load_draft_picks(client)
    made = [p for p in picks if p.is_made]
    console.print(f"In progress: {detail.get('inProgress', False)}")
    console.print(f"Drafted: {detail.get('drafted', False)}")
    console.print(f"Picks made: {len(made)}/{len(picks)}")
    if made:
        last = made[-1]
        console.print(f"Last pick: #{last.overall_pick} team={last.team_id} player_id={last.player_id}")


@draft_app.command("watch")
def draft_watch(
    league_id: int | None = typer.Option(
        None,
        "--league-id",
        help="Override configured ESPN league ID (useful for mock drafts)",
    ),
    show_clock: bool = typer.Option(
        False,
        "--show-clock",
    ),
    show_unknown: bool = typer.Option(
        False,
        "--show-unknown",
    ),
) -> None:
    settings = get_settings()

    client = ESPNClient(
        settings,
        league_id=league_id,
    )

    watch_live_draft(
        client,
        team_id=settings.espn_team_id,
        show_clock=show_clock,
        show_unknown=show_unknown,
    )

    console = Console()

    effective_league_id = (
        league_id
        if league_id is not None
        else settings.espn_league_id
    )

    console.print(
        f"[bold]Watching ESPN draft[/bold] "
        f"league={effective_league_id} "
        f"team={settings.espn_team_id}"
    )

    seen: dict[int, int] = {}

    try:
        while True:
            draft_data, picks = load_draft_picks(client)

            drafted = bool(draft_data.get("drafted"))
            in_progress = bool(draft_data.get("inProgress"))

            for pick in picks:
                # ESPN uses -1 for an unfilled draft slot.
                if pick.player_id <= 0:
                    continue

                previous = seen.get(pick.overall_pick)

                if previous == pick.player_id:
                    continue

                seen[pick.overall_pick] = pick.player_id

                ours = (
                    " [bold green]THE RESERVISTS[/bold green]"
                    if pick.team_id == settings.espn_team_id
                    else ""
                )

                console.print(
                    f"Pick {pick.overall_pick:>3} "
                    f"(R{pick.round_id}.{pick.round_pick}) "
                    f"Team {pick.team_id:>2} "
                    f"→ ESPN player {pick.player_id}"
                    f"{ours}"
                )

            filled = sum(
                1
                for pick in picks
                if pick.player_id > 0
            )

            if drafted:
                console.print(
                    f"\n[green]Draft complete. "
                    f"{filled}/{len(picks)} picks filled.[/green]"
                )
                break

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        console.print("\n[yellow]Draft watcher stopped.[/yellow]")


@draft_app.command("select")
def draft_select(
    player_id: int = typer.Argument(...),
    league_id: int | None = typer.Option(None, "--league-id"),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings, league_id=league_id)

    with DraftSession(
        client,
        team_id=settings.espn_team_id,
    ) as session:
        console = Console()
        console.print(
            f"Connected. Submitting ESPN player {player_id} "
            "when explicitly requested..."
        )
        selected = session.select_and_wait(player_id)
        console.print(
            f"[green]ESPN acknowledged player {selected.player_id} "
            f"for team {selected.team_id}.[/green]"
        )


@draft_app.command("run")
def draft_run(
    league_id: int | None = typer.Option(
        None,
        "--league-id",
        help="Override league ID for ESPN mock drafts",
    ),
    auto_select: bool | None = typer.Option(
        None,
        "--auto-select/--no-auto-select",
        help="Allow Fantasy GM to submit its recommendation through ESPN",
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings, league_id=league_id)

    if auto_select is None:
        # Keep compatibility with the existing environment flag.
        import os
        auto_select = (
            os.getenv("FANTASY_GM_AUTO_SELECT", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

    runner = DraftRunner(
        client,
        team_id=settings.espn_team_id,
        favorite_team=settings.favorite_team,
        fandom_weight=settings.fandom_weight,
        config=DraftRunnerConfig(
            auto_select=auto_select,
            ack_timeout=3.0,
            board_pool_limit=100,
        ),
    )

    runner.run()
