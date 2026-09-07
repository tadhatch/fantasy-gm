from __future__ import annotations


import typer
from rich.console import Console
from rich.table import Table

from fantasy_gm.config import get_settings
from fantasy_gm.draft.watcher import watch_draft
from fantasy_gm.draft.watcher_live import watch_live_draft
from fantasy_gm.draft.runner import DraftRunner, DraftRunnerConfig
from fantasy_gm.draft.session import DraftSession
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import LINEUP_SLOT_IDS, POSITION_IDS
from fantasy_gm.espn.draft import load_draft_picks
from fantasy_gm.espn.league import format_draft_time, load_league_summary
from fantasy_gm.espn.players import load_players
from fantasy_gm.board.builder import build_board
from fantasy_gm.board.renderer import render_board
from fantasy_gm.context.renderer import render_context
from fantasy_gm.context.service import refresh_context
from fantasy_gm.chatbot.cli_commands import build_chatbot_typer
from fantasy_gm.espn.roster import (
    current_scoring_period,
    load_team_roster,
)
from fantasy_gm.espn.transactions import ESPNTransactionsClient, LineupMove
from fantasy_gm.supervisor import run_supervisor


# Build Typers
app = typer.Typer(no_args_is_help=True)
console = Console()
context_app = typer.Typer(help="AI current-context research")
app.add_typer(context_app, name="context")
chatbot_app = build_chatbot_typer()
app.add_typer(chatbot_app, name="chatbot")
worker_app = typer.Typer(no_args_is_help=True, help="Run one-shot GM workers")
app.add_typer(worker_app, name="worker")
roster_app = typer.Typer(
    no_args_is_help=True,
    help="Roster reads and roster/waiver/trade transactions",
)
app.add_typer(roster_app, name="roster")


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


@roster_app.command("show")
def roster_show(
    team_id: int | None = typer.Option(
        None,
        "--team-id",
        help="Defaults to your configured team",
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    roster = load_team_roster(client, team_id or settings.espn_team_id)

    table = Table(title=f"{roster.team_name} — roster")
    table.add_column("Player ID", justify="right")
    table.add_column("Player")
    table.add_column("Slot", justify="right")
    table.add_column("Injury")
    for entry in roster.entries:
        table.add_row(
            str(entry.player_id),
            entry.name,
            str(entry.lineup_slot_id),
            entry.injury_status or "-",
        )
    console.print(table)


@roster_app.command("set-lineup")
def roster_set_lineup(
    move: list[str] = typer.Option(
        ...,
        "--move",
        help="playerId:fromSlotId:toSlotId, repeatable",
    ),
    team_id: int | None = typer.Option(None, "--team-id"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Actually submit (also requires FANTASY_GM_TRANSACTIONS_MODE=live)",
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    txn = ESPNTransactionsClient(client)

    moves = []
    for raw in move:
        player_id, from_slot, to_slot = raw.split(":")
        moves.append(
            LineupMove(
                player_id=int(player_id),
                from_slot_id=int(from_slot),
                to_slot_id=int(to_slot),
            )
        )

    period = current_scoring_period(client)
    txn.set_lineup(
        team_id=team_id or settings.espn_team_id,
        moves=moves,
        scoring_period_id=period,
        confirm=confirm,
    )


@roster_app.command("add-drop")
def roster_add_drop(
    add: int | None = typer.Option(None, "--add"),
    drop: int | None = typer.Option(None, "--drop"),
    team_id: int | None = typer.Option(None, "--team-id"),
    waiver: bool = typer.Option(False, "--waiver"),
    bid: int | None = typer.Option(None, "--bid"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Actually submit (also requires FANTASY_GM_TRANSACTIONS_MODE=live)",
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    txn = ESPNTransactionsClient(client)

    period = current_scoring_period(client)
    txn.add_drop(
        team_id=team_id or settings.espn_team_id,
        add_player_id=add,
        drop_player_id=drop,
        scoring_period_id=period,
        via_waiver=waiver,
        bid_amount=bid,
        confirm=confirm,
    )


@roster_app.command("propose-trade")
def roster_propose_trade(
    to_team_id: int = typer.Option(..., "--to"),
    offer: list[int] = typer.Option(
        ..., "--offer", help="Player ID you are giving up, repeatable"
    ),
    request: list[int] = typer.Option(
        ..., "--request", help="Player ID you are asking for, repeatable"
    ),
    message: str = typer.Option("", "--message"),
    team_id: int | None = typer.Option(None, "--team-id"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Actually submit (also requires FANTASY_GM_TRANSACTIONS_MODE=live)",
    ),
) -> None:
    settings = get_settings()
    client = ESPNClient(settings)
    txn = ESPNTransactionsClient(client)

    period = current_scoring_period(client)
    txn.propose_trade(
        proposing_team_id=team_id or settings.espn_team_id,
        receiving_team_id=to_team_id,
        players_offered=offer,
        players_requested=request,
        scoring_period_id=period,
        message=message,
        confirm=confirm,
    )


@worker_app.command("test")
def worker_test(
    job_id: str = typer.Option(..., "--job-id"),
    sleep_seconds: int = typer.Option(
        240,
        "--sleep",
    ),
) -> None:
    from fantasy_gm.workers.test_worker import run_test_worker

    run_test_worker(
        job_id=job_id,
        sleep_seconds=sleep_seconds,
    )


@worker_app.command("evaluate")
def worker_evaluate(
    team_id: int | None = typer.Option(
        None,
        "--team-id",
        help="Defaults to your configured team",
    ),
    pool_size: int = typer.Option(
        50,
        "--pool-size",
        help="Top-owned free agents to keep in the shared inventory alongside the roster",
    ),
    max_calls: int = typer.Option(
        5,
        "--max-calls",
        help="Broad-scan + escalation call budget for this run (real OpenAI API cost)",
    ),
    freshness_hours: int = typer.Option(
        24,
        "--freshness-hours",
        help="Skip players evaluated more recently than this",
    ),
) -> None:
    """
    Refresh the off-field "feeling" signal (injury/contract/news/
    distraction) for the roster + top free agents, storing results in
    Postgres as the shared inventory other workers (lineup, waiver,
    trade) read instead of researching players themselves. One broad
    web-search scan across the whole pool, escalating to a real
    per-player deep dive only for whatever it flags as significant or
    unclear — not one call per player. Makes real OpenAI API calls.
    """
    from fantasy_gm.context.evaluation_worker import evaluate_players

    settings = get_settings()
    client = ESPNClient(settings)

    def progress(stale_count: int, total: int) -> None:
        console.print(
            f"Scanning {stale_count}/{total} players "
            "not already freshly evaluated..."
        )

    results = evaluate_players(
        client,
        team_id=team_id or settings.espn_team_id,
        free_agent_pool_size=pool_size,
        max_calls=max_calls,
        freshness_hours=freshness_hours,
        progress=progress,
    )

    console.print(f"[green]Evaluated {len(results)} player(s).[/green]")


@worker_app.command("waiver")
def worker_waiver(
    team_id: int | None = typer.Option(
        None,
        "--team-id",
        help="Defaults to your configured team",
    ),
    pool_size: int = typer.Option(
        60,
        "--pool-size",
        help="Free agents fed to the screening stage after Python pre-filtering",
    ),
    shortlist_size: int = typer.Option(
        15,
        "--shortlist-size",
        help="How many free agents the screening stage shortlists",
    ),
    deep_dive_budget: int = typer.Option(
        3,
        "--deep-dive-budget",
        help="Reserve fresh web-search calls the decision stage may spend",
    ),
    no_submit: bool = typer.Option(
        False,
        "--no-submit",
        help="Skip the transaction call entirely (still respects shadow mode either way)",
    ),
) -> None:
    """
    Roster evaluation -> free-agent screening -> deep evaluation ->
    GM decision. 4 OpenAI calls plus up to --deep-dive-budget more for
    fresh research the decision stage specifically asks for. Submits via
    the transactions client, which shadows unless FANTASY_GM_TRANSACTIONS_MODE
    is live AND is passed confirm=True — this pipeline never passes
    confirm=True itself, per the "shadow everything first" policy.
    """
    from fantasy_gm.waiver.pipeline import run_waiver_pipeline

    settings = get_settings()
    client = ESPNClient(settings)

    result = run_waiver_pipeline(
        client,
        team_id=team_id or settings.espn_team_id,
        free_agent_pool_size=pool_size,
        shortlist_size=shortlist_size,
        deep_dive_budget=deep_dive_budget,
        attempt_transaction=not no_submit,
    )

    console.print(
        f"[bold]Roster summary:[/bold] {result.roster_analysis.summary}"
    )
    console.print(
        f"[bold]Positional needs:[/bold] "
        f"{', '.join(result.roster_analysis.positional_needs) or 'none'}"
    )
    console.print(
        f"[bold]Shortlist:[/bold] {len(result.shortlist)} candidates"
    )
    console.print(
        f"[bold]Candidate moves:[/bold] {len(result.candidate_moves)}"
    )
    console.print(
        f"[bold]Deep dives used:[/bold] "
        f"{len(result.deep_dives_used)}/{deep_dive_budget}"
    )
    console.print()
    console.print(
        f"[bold cyan]GM decision:[/bold cyan] {result.decision.action}"
    )
    if result.decision.action == "add_drop":
        console.print(
            f"  ADD {result.decision.add_player_name} "
            f"({result.decision.add_player_id}) / "
            f"DROP {result.decision.drop_player_name} "
            f"({result.decision.drop_player_id})"
        )
    console.print(f"  reasoning: {result.decision.reasoning}")
    console.print(f"  confidence: {result.decision.confidence:.0%}")
    console.print()
    console.print(
        f"[green]Calls used: {result.calls_used}. "
        f"Executed live: {result.executed}[/green]"
    )


@worker_app.command("lineup")
def worker_lineup(
    team_id: int | None = typer.Option(
        None,
        "--team-id",
        help="Defaults to your configured team",
    ),
    week: int | None = typer.Option(
        None,
        "--week",
        help="Defaults to the current scoring period",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Actually submit (also requires FANTASY_GM_TRANSACTIONS_MODE=live)",
    ),
) -> None:
    """
    Set the optimal starting lineup for the week: ESPN's own weekly
    projections, discounted for injury status (OUT/IR/suspended players
    never start over an available alternative), plus whatever real-world
    evaluation is cached for each roster player. No web-search/LLM calls
    of its own — it reads what the evaluation worker already produced.
    """
    from fantasy_gm.lineup.service import build_lineup_plan, submit_lineup_plan

    settings = get_settings()
    client = ESPNClient(settings)
    resolved_team_id = team_id or settings.espn_team_id

    plan, resolved_week = build_lineup_plan(
        client,
        team_id=resolved_team_id,
        week=week,
    )

    console.print(f"[bold]Week {resolved_week} lineup plan[/bold]")

    def slot_name(slot_id: int) -> str:
        return LINEUP_SLOT_IDS.get(slot_id, str(slot_id))

    def score_breakdown(player_id: int) -> str:
        candidate = plan.candidates.get(player_id)
        if candidate is None:
            return "?"
        raw = (
            f"{candidate.raw_projection:.1f}"
            if candidate.raw_projection is not None
            else "—"
        )
        delta = candidate.evaluation_delta
        delta_str = f"{delta:+.1f}" if delta else "+0.0"
        return f"{candidate.projected_points:.1f} ({raw} espn {delta_str} eval)"

    if not plan.moves:
        console.print(
            "[green]Lineup is already optimal — no moves needed.[/green]"
        )
    else:
        table = Table(title="Proposed moves — why each one happened")
        table.add_column("Player")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Score (espn + eval)")
        for move in plan.moves:
            candidate = plan.candidates.get(move.player_id)
            name = candidate.entry.name if candidate else str(move.player_id)
            table.add_row(
                name,
                slot_name(move.from_slot_id),
                slot_name(move.to_slot_id),
                score_breakdown(move.player_id),
            )
        console.print(table)

    board = Table(title="Full roster board")
    board.add_column("Player")
    board.add_column("Slot")
    board.add_column("Score (espn + eval)")
    board.add_column("Status")
    for candidate in sorted(
        plan.candidates.values(),
        key=lambda c: c.projected_points,
        reverse=True,
    ):
        slot_id = plan.assignments.get(
            candidate.entry.player_id, candidate.entry.lineup_slot_id
        )
        status_bits = []
        if candidate.locked:
            status_bits.append("locked")
        if candidate.unavailable_reason:
            status_bits.append(candidate.unavailable_reason)
        status = ", ".join(status_bits) if status_bits else "ok"

        board.add_row(
            candidate.entry.name,
            slot_name(slot_id),
            score_breakdown(candidate.entry.player_id),
            status,
        )
    console.print(board)

    for note in plan.notes:
        console.print(f"[yellow]note:[/yellow] {note}")

    submit_lineup_plan(
        client,
        team_id=resolved_team_id,
        plan=plan,
        week=resolved_week,
        confirm=confirm,
    )