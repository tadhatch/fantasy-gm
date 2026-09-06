from __future__ import annotations

import os
import time
import threading

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.league import load_league_summary
from fantasy_gm.railway.services import RailwayServiceManager


console = Console()


def _poll_seconds() -> int:
    return int(os.getenv("FANTASY_GM_SUPERVISOR_POLL_SECONDS", "60"))


def _chatbot_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_CHATBOT_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


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
    service_manager.log_services()

    # Sweep up anything a previous supervisor run launched and never got
    # to clean up itself (e.g. it was redeployed/restarted mid-job).
    # RAILWAY_SERVICE_NAME is injected automatically by Railway; it's None
    # when running outside Railway (e.g. locally), in which case there's
    # nothing to protect beyond the chatbot.
    protected_services = {os.getenv("FANTASY_GM_CHATBOT_SERVICE", "chatbot")}
    own_service_name = os.getenv("RAILWAY_SERVICE_NAME")
    if own_service_name:
        protected_services.add(own_service_name)

    orphans_deleted = service_manager.cleanup_finished_workers(
        exclude=protected_services
    )
    if orphans_deleted:
        console.print(
            f"[yellow][WORKER][/yellow] "
            f"cleaned up {len(orphans_deleted)} orphaned finished "
            f"service(s) from a previous run: {', '.join(orphans_deleted)}"
        )

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
    service_manager.log_services()

    poll_seconds = _poll_seconds()

    console.print()
    console.print(
        f"[green]Supervisor initialization complete.[/green] "
        f"Polling every {poll_seconds}s."
    )

    while True:
        try:
            # For now, periodically report the controlled service state.
            #
            # Later this loop will also:
            # - watch for upcoming draft activity
            # - dispatch the draft worker
            # - schedule waiver analysis
            # - schedule lineup optimization
            # - detect incoming trades
            #
            # Railway worker lifecycle monitoring happens independently
            # in its cleanup thread.

            service_manager.log_services()

        except Exception as exc:
            console.print(
                f"[red]Supervisor error: {exc!r}[/red]"
            )

        time.sleep(poll_seconds)
