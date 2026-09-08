from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timedelta, timezone

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.league import load_league_summary
from fantasy_gm.nfl_schedule import NFL_TZ, load_season_games
from fantasy_gm.railway.services import (
    RailwayServiceManager,
    has_actually_finished,
)


console = Console()

EVALUATION_WORKER_PREFIX = "worker-evaluate-"
LINEUP_WORKER_PREFIX = "worker-lineup-"
WAIVER_WORKER_PREFIX = "worker-waiver-"
INCOMING_TRADE_WORKER_PREFIX = "worker-trade-respond-"

# How long before a wave of kickoffs the lineup should already be set.
LINEUP_CHECKPOINT_BUFFER_MINUTES = 75

# How long after this league's own waiverProcessHour (read live from ESPN,
# not hardcoded — see _waiver_process_hour) to run the waiver worker.
WAIVER_CHECKPOINT_BUFFER_MINUTES = 30
DEFAULT_WAIVER_PROCESS_HOUR = 11  # fallback only, if ESPN can't be reached

_WAIVER_PROCESS_HOUR_CACHE_TTL_SECONDS = 6 * 3600
_waiver_process_hour_cache: tuple[float, int] | None = None


def _poll_seconds() -> int:
    return int(os.getenv("FANTASY_GM_SUPERVISOR_POLL_SECONDS", "60"))


def _chatbot_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_CHATBOT_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _safe_log_services(service_manager: RailwayServiceManager) -> None:
    """
    log_services() talks to Railway's API and has no retry of its own
    beyond RailwayClient's connection/timeout handling — an auth hiccup
    (e.g. a just-rotated token still propagating), rate limit, or other
    transient failure should be logged and skipped, not take down the
    whole supervisor process.
    """
    try:
        service_manager.log_services()
    except Exception as exc:
        console.print(
            f"[red]Failed to list Railway services: {exc!r}[/red]"
        )


def _protected_services() -> set[str]:
    # RAILWAY_SERVICE_NAME is injected automatically by Railway; it's None
    # when running outside Railway (e.g. locally), in which case there's
    # nothing to protect beyond the chatbot.
    protected = {os.getenv("FANTASY_GM_CHATBOT_SERVICE", "chatbot")}
    own_service_name = os.getenv("RAILWAY_SERVICE_NAME")
    if own_service_name:
        protected.add(own_service_name)
    return protected


def _sweep_orphans(service_manager: RailwayServiceManager) -> None:
    """
    Delete any controlled worker that has already finished.

    This has to run continuously, not just once at startup: a worker's
    wait_and_delete() thread lives only in the process that launched it,
    so if that supervisor process gets redeployed/restarted while a
    worker is still running, the thread watching it dies too — the
    worker is orphaned with nothing left to notice when it finishes.
    Running this sweep on every poll cycle means such a worker still
    gets caught (and deleted) within one interval instead of sitting
    there until the next full supervisor restart happens to catch it.
    """
    try:
        deleted = service_manager.cleanup_finished_workers(
            exclude=_protected_services()
        )
        if deleted:
            console.print(
                f"[yellow][WORKER][/yellow] "
                f"cleaned up {len(deleted)} orphaned finished "
                f"service(s): {', '.join(deleted)}"
            )
    except Exception as exc:
        console.print(
            f"[bold red][WORKER][/bold red] "
            f"Failed to sweep orphaned services: {exc!r}"
        )


def _evaluation_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_EVALUATION_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _worker_already_running(
    service_manager: RailwayServiceManager, prefix: str
) -> bool:
    try:
        services = service_manager.client.list_services()
    except Exception:
        # Can't tell either way — err toward not double-launching.
        return True

    # A worker counts as "still running" unless it has genuinely
    # finished — checking `deployment_stopped` alone has the same false
    # positive has_actually_finished() exists to avoid: it reads True
    # for a deployment attempt that was abandoned/superseded before ever
    # actually starting, not just for one that ran to completion. Without
    # this, a still-legitimately-deploying worker can look "not running"
    # and get double-dispatched.
    return any(
        s.name.startswith(prefix) and not has_actually_finished(s)
        for s in services
    )


def _waiver_process_hour(client: ESPNClient) -> int:
    """
    This league's actual waiver-processing hour (ET), read live from
    ESPN's own settings rather than assumed — leagues can and do
    configure this differently, and a commissioner can change it
    mid-season. Cached for a few hours since it's a league setting, not
    something that changes minute to minute, and this gets checked every
    supervisor poll.
    """
    global _waiver_process_hour_cache

    now_ts = time.monotonic()
    if (
        _waiver_process_hour_cache
        and now_ts - _waiver_process_hour_cache[0]
        < _WAIVER_PROCESS_HOUR_CACHE_TTL_SECONDS
    ):
        return _waiver_process_hour_cache[1]

    try:
        data = client.get_league(["mSettings"])
        hour = int(
            data["settings"]["acquisitionSettings"]["waiverProcessHour"]
        )
    except Exception as exc:
        hour = (
            _waiver_process_hour_cache[1]
            if _waiver_process_hour_cache
            else DEFAULT_WAIVER_PROCESS_HOUR
        )
        console.print(
            f"[red]Failed to read waiver process hour, "
            f"using {hour}: {exc!r}[/red]"
        )

    _waiver_process_hour_cache = (now_ts, hour)
    return hour


def _most_recent_waiver_checkpoint(
    now: datetime, client: ESPNClient
) -> datetime:
    """
    Most recent daily checkpoint that has already passed —
    WAIVER_CHECKPOINT_BUFFER_MINUTES after this league's own
    waiverProcessHour, so each run sees the prior day's claims already
    resolved (a fresh free-agent pool) and gets maximum standing before
    the *next* processing window, rather than being submitted right
    before a cutoff. Runs every calendar day, including days this league
    doesn't actually process waivers on (e.g. Tuesday here) — harmless: a
    claim made on an off day just sits until the next real processing
    day, and the daily reasoning refresh is still worth having regardless
    of whether that day happens to process.
    """
    now_local = now.astimezone(NFL_TZ)
    hour = _waiver_process_hour(client)
    base = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    checkpoint_today = base + timedelta(
        minutes=WAIVER_CHECKPOINT_BUFFER_MINUTES
    )

    if checkpoint_today <= now_local:
        return checkpoint_today
    return checkpoint_today - timedelta(days=1)


def _waiver_due(client: ESPNClient) -> bool:
    from fantasy_gm.railway.task_runs import TaskRunStore

    try:
        last_run = TaskRunStore().last_run_at("waiver")
    except Exception as exc:
        console.print(
            f"[red]Failed to check waiver schedule: {exc!r}[/red]"
        )
        return False

    checkpoint = _most_recent_waiver_checkpoint(
        datetime.now(timezone.utc), client
    )

    if last_run is None:
        return True

    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    return last_run < checkpoint


def _evaluation_due(interval_hours: float) -> bool:
    from fantasy_gm.context.postgres_store import PostgresContextStore

    try:
        latest = PostgresContextStore().latest_researched_at()
    except Exception as exc:
        console.print(
            f"[red]Failed to check evaluation freshness: {exc!r}[/red]"
        )
        return False

    if latest is None:
        return True

    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)

    elapsed = datetime.now(timezone.utc) - latest
    return elapsed.total_seconds() >= interval_hours * 3600


def _dispatch_evaluation_worker(service_manager: RailwayServiceManager) -> None:
    pool_size = int(os.getenv("FANTASY_GM_EVALUATION_POOL_SIZE", "50"))
    max_calls = int(os.getenv("FANTASY_GM_EVALUATION_MAX_CALLS", "5"))
    dst_max_calls = int(os.getenv("FANTASY_GM_EVALUATION_DST_MAX_CALLS", "2"))
    freshness_hours = int(
        os.getenv("FANTASY_GM_EVALUATION_FRESHNESS_HOURS", "24")
    )

    try:
        worker = service_manager.launch_evaluation_worker(
            pool_size=pool_size,
            max_calls=max_calls,
            dst_max_calls=dst_max_calls,
            freshness_hours=freshness_hours,
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"Starting lifecycle monitor for {worker.name}"
        )

        # Rough estimate only — deployment_stopped is the real completion
        # signal; this just sets wait_and_delete's safety-net ceiling.
        # The first call alone is a broad web-search scan across the
        # whole pool, which can take a while by itself; escalations add
        # more on top. Errs generous.
        expected_runtime_seconds = max(
            300, (max_calls + dst_max_calls) * 60
        )

        cleanup_thread = threading.Thread(
            target=service_manager.wait_and_delete,
            args=(worker,),
            kwargs={
                "expected_runtime_seconds": expected_runtime_seconds
            },
            daemon=True,
            name=f"cleanup-{worker.name}",
        )
        cleanup_thread.start()

    except Exception as exc:
        console.print(
            f"[bold red][WORKER][/bold red] "
            f"Failed to launch evaluation worker: {exc!r}"
        )


def _lineup_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_LINEUP_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _most_recent_lineup_checkpoint(
    now: datetime, *, season: int
) -> datetime | None:
    now_local = now.astimezone(NFL_TZ)
    buffer = timedelta(minutes=LINEUP_CHECKPOINT_BUFFER_MINUTES)

    try:
        games = load_season_games(season)
    except Exception as exc:
        console.print(
            f"[red]Failed to load NFL schedule for lineup "
            f"checkpoints: {exc!r}[/red]"
        )
        return None

    passed = [
        game.kickoff - buffer
        for game in games
        if game.kickoff - buffer <= now_local
    ]

    return max(passed) if passed else None


def _lineup_due(*, season: int) -> bool:
    """
    Due once per wave of kickoffs (a checkpoint some buffer before the
    earliest game of a slate), not on a fixed interval — a lineup set at
    3 AM Wednesday has nothing new to react to; one set right before
    kickoff does.
    """
    from fantasy_gm.railway.task_runs import TaskRunStore

    try:
        last_run = TaskRunStore().last_run_at("lineup")
    except Exception as exc:
        console.print(
            f"[red]Failed to check lineup schedule: {exc!r}[/red]"
        )
        return False

    checkpoint = _most_recent_lineup_checkpoint(
        datetime.now(timezone.utc), season=season
    )
    if checkpoint is None:
        return False  # no known schedule data — don't guess

    if last_run is None:
        return True

    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    return last_run < checkpoint


def _dispatch_lineup_worker(service_manager: RailwayServiceManager) -> None:
    confirm = (
        os.getenv("FANTASY_GM_LINEUP_CONFIRM", "false")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    try:
        worker = service_manager.launch_lineup_worker(confirm=confirm)

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"Starting lifecycle monitor for {worker.name}"
        )

        # No LLM calls here, just a handful of ESPN reads and one
        # transaction call — should be quick, but still generous since
        # deployment_stopped remains the real completion signal.
        expected_runtime_seconds = 120

        cleanup_thread = threading.Thread(
            target=service_manager.wait_and_delete,
            args=(worker,),
            kwargs={
                "expected_runtime_seconds": expected_runtime_seconds
            },
            daemon=True,
            name=f"cleanup-{worker.name}",
        )
        cleanup_thread.start()

    except Exception as exc:
        console.print(
            f"[bold red][WORKER][/bold red] "
            f"Failed to launch lineup worker: {exc!r}"
        )


def _waiver_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_WAIVER_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _dispatch_waiver_worker(service_manager: RailwayServiceManager) -> None:
    pool_size = int(os.getenv("FANTASY_GM_WAIVER_POOL_SIZE", "60"))
    shortlist_size = int(os.getenv("FANTASY_GM_WAIVER_SHORTLIST_SIZE", "15"))
    deep_dive_budget = int(
        os.getenv("FANTASY_GM_WAIVER_DEEP_DIVE_BUDGET", "3")
    )
    confirm = (
        os.getenv("FANTASY_GM_WAIVER_CONFIRM", "false")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    try:
        worker = service_manager.launch_waiver_worker(
            pool_size=pool_size,
            shortlist_size=shortlist_size,
            deep_dive_budget=deep_dive_budget,
            confirm=confirm,
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"Starting lifecycle monitor for {worker.name}"
        )

        # 4 core reasoning calls plus up to deep_dive_budget more —
        # same shape as the evaluation worker's estimate, errs generous
        # since deployment_stopped remains the real completion signal.
        expected_runtime_seconds = max(300, (4 + deep_dive_budget) * 60)

        cleanup_thread = threading.Thread(
            target=service_manager.wait_and_delete,
            args=(worker,),
            kwargs={
                "expected_runtime_seconds": expected_runtime_seconds
            },
            daemon=True,
            name=f"cleanup-{worker.name}",
        )
        cleanup_thread.start()

    except Exception as exc:
        console.print(
            f"[bold red][WORKER][/bold red] "
            f"Failed to launch waiver worker: {exc!r}"
        )


def _incoming_trade_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_INCOMING_TRADE_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _incoming_trade_auto_dispatch_enabled() -> bool:
    # Separate from _incoming_trade_enabled(): that one gates detection
    # (and its log line) entirely, this one only gates whether a detected
    # trade also gets an evaluation worker dispatched automatically.
    # Turning this off while leaving detection on is useful on its own —
    # e.g. confirming the supervisor actually sees a real incoming trade
    # before trusting it to also act on one.
    return (
        os.getenv("FANTASY_GM_INCOMING_TRADE_AUTO_DISPATCH", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _dispatch_incoming_trade_worker(
    service_manager: RailwayServiceManager, trade
) -> None:
    deep_dive_budget = int(
        os.getenv("FANTASY_GM_INCOMING_TRADE_DEEP_DIVE_BUDGET", "1")
    )

    try:
        worker = service_manager.launch_incoming_trade_worker(
            trade_id=trade.trade_id,
            deep_dive_budget=deep_dive_budget,
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"Starting lifecycle monitor for {worker.name}"
        )

        # One reasoning call plus up to deep_dive_budget more.
        expected_runtime_seconds = max(180, (1 + deep_dive_budget) * 60)

        cleanup_thread = threading.Thread(
            target=service_manager.wait_and_delete,
            args=(worker,),
            kwargs={
                "expected_runtime_seconds": expected_runtime_seconds
            },
            daemon=True,
            name=f"cleanup-{worker.name}",
        )
        cleanup_thread.start()

    except Exception as exc:
        console.print(
            f"[bold red][WORKER][/bold red] "
            f"Failed to launch incoming-trade worker: {exc!r}"
        )


def _check_incoming_trades(
    service_manager: RailwayServiceManager, client: ESPNClient
) -> None:
    """
    Polls for trade proposals other teams have sent us, logs each
    not-yet-decided one it sees, and (if
    _incoming_trade_auto_dispatch_enabled()) dispatches an evaluation
    worker for it. Unlike the other recurring workers, there's no single
    "is it due" question — any number of distinct pending trades could
    exist at once, so each is tracked independently by its own ESPN
    transaction id rather than a single last-run timestamp.
    """
    from fantasy_gm.trade.incoming import find_pending_incoming_trades
    from fantasy_gm.trade.store import IncomingTradeDecisionStore

    try:
        pending = find_pending_incoming_trades(
            client, team_id=client.settings.espn_team_id
        )
    except Exception as exc:
        console.print(
            f"[red]Failed to check incoming trades: {exc!r}[/red]"
        )
        return

    if not pending:
        return

    try:
        decision_store = IncomingTradeDecisionStore()
    except Exception as exc:
        console.print(
            f"[red]Failed to open incoming trade decision store: "
            f"{exc!r}[/red]"
        )
        return

    for trade in pending:
        try:
            if decision_store.has_decided(trade.trade_id):
                continue
        except Exception as exc:
            console.print(
                f"[red]Failed to check decision store for trade "
                f"{trade.trade_id}: {exc!r}[/red]"
            )
            continue

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"incoming trade detected: {trade.trade_id} from "
            f"{trade.proposing_team_name} "
            f"(offered: {trade.offered_to_us_ids}, "
            f"requested: {trade.requested_from_us_ids})"
        )

        if not _incoming_trade_auto_dispatch_enabled():
            continue

        prefix = f"{INCOMING_TRADE_WORKER_PREFIX}{trade.trade_id[:8]}"
        if _worker_already_running(service_manager, prefix):
            continue

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"dispatching evaluation worker for trade {trade.trade_id}"
        )
        _dispatch_incoming_trade_worker(service_manager, trade)


def run_supervisor(client: ESPNClient) -> None:
    console.print(
        "[bold green]Fantasy GM supervisor started in GM mode[/bold green]"
    )

    summary, _ = load_league_summary(client)

    console.print(
        f"League: [bold]{summary.name}[/bold] "
        f"| Team: {summary.team_name} "
        f"| Team ID: {summary.team_id}"
    )

    service_manager = RailwayServiceManager()

    console.print()
    console.print(
        "[bold cyan]Initializing Railway-managed services[/bold cyan]"
    )

    # Show everything currently visible to the supervisor.
    _safe_log_services(service_manager)

    # Sweep up anything a previous supervisor run launched and never got
    # to clean up itself (e.g. it was redeployed/restarted mid-job). Also
    # runs every poll cycle below, not just here — see _sweep_orphans().
    _sweep_orphans(service_manager)

    # The chatbot is no longer a child process of the supervisor.
    # Ensure it exists as its own persistent Railway service.
    if _chatbot_enabled():
        try:
            service_manager.ensure_chatbot()
        except Exception as exc:
            console.print(
                f"[bold red][CHATBOT][/bold red] "
                f"Failed to ensure chatbot service: {exc!r}"
            )
    else:
        console.print(
            "[yellow][CHATBOT][/yellow] "
            "Disabled by FANTASY_GM_CHATBOT_ENABLED"
        )

    # Temporary integration test.
    # This will eventually be removed and replaced by actual event-driven jobs.
    test_worker_on_start = (
        os.getenv(
            "FANTASY_GM_TEST_WORKER_ON_START",
            "false",
        )
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    if test_worker_on_start:
        try:
            test_worker_sleep_seconds = int(
                os.getenv("FANTASY_GM_TEST_WORKER_SLEEP_SECONDS", "240")
            )

            worker = service_manager.launch_test_worker(
                sleep_seconds=test_worker_sleep_seconds
            )

            console.print(
                f"[cyan][WORKER][/cyan] "
                f"Starting lifecycle monitor for {worker.name}"
            )

            cleanup_thread = threading.Thread(
                target=service_manager.wait_and_delete,
                args=(worker,),
                kwargs={
                    "expected_runtime_seconds": test_worker_sleep_seconds
                },
                daemon=True,
                name=f"cleanup-{worker.name}",
            )
            cleanup_thread.start()

        except Exception as exc:
            console.print(
                f"[bold red][WORKER][/bold red] "
                f"Failed to launch test worker: {exc!r}"
            )

    console.print()
    _safe_log_services(service_manager)

    poll_seconds = _poll_seconds()

    console.print()
    console.print(
        f"[green]Supervisor initialization complete.[/green] "
        f"Polling every {poll_seconds}s."
    )

    while True:
        try:
            # Later this loop will also:
            # - watch for upcoming draft activity
            # - dispatch the draft worker

            service_manager.log_services()
            _sweep_orphans(service_manager)

            if _evaluation_enabled():
                interval_hours = float(
                    os.getenv("FANTASY_GM_EVALUATION_INTERVAL_HOURS", "24")
                )
                if _evaluation_due(interval_hours) and not _worker_already_running(
                    service_manager, EVALUATION_WORKER_PREFIX
                ):
                    console.print(
                        "[cyan][WORKER][/cyan] "
                        "player evaluation due; dispatching"
                    )
                    _dispatch_evaluation_worker(service_manager)

            if _lineup_enabled():
                if _lineup_due(
                    season=client.settings.espn_season
                ) and not _worker_already_running(
                    service_manager, LINEUP_WORKER_PREFIX
                ):
                    console.print(
                        "[cyan][WORKER][/cyan] "
                        "lineup check due; dispatching"
                    )
                    _dispatch_lineup_worker(service_manager)

            if _waiver_enabled():
                if _waiver_due(client) and not _worker_already_running(
                    service_manager, WAIVER_WORKER_PREFIX
                ):
                    console.print(
                        "[cyan][WORKER][/cyan] "
                        "waiver analysis due; dispatching"
                    )
                    _dispatch_waiver_worker(service_manager)

            if _incoming_trade_enabled():
                _check_incoming_trades(service_manager, client)

        except Exception as exc:
            console.print(
                f"[red]Supervisor error: {exc!r}[/red]"
            )

        time.sleep(poll_seconds)
