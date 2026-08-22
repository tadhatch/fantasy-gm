from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.draft import load_draft_picks
from fantasy_gm.espn.league import load_league_summary


POLL_SECONDS = 10
DRAFT_LEAD_TIME = timedelta(minutes=5)

console = Console()


def _chatbot_enabled() -> bool:
    return (
        os.getenv("FANTASY_GM_CHATBOT_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def _launch_chatbot_process() -> subprocess.Popen | None:
    if not _chatbot_enabled():
        return None

    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fantasy_gm.chatbot.entrypoint",
        ]
    )


def _stop_process(
    process: subprocess.Popen | None,
    *,
    name: str,
    timeout: float = 10.0,
) -> None:
    if process is None or process.poll() is not None:
        return

    console.print(f"[yellow]Stopping {name}[/yellow]")
    process.terminate()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        console.print(
            f"[yellow]{name} did not stop cleanly; killing it[/yellow]"
        )
        process.kill()
        process.wait()


def run_supervisor(client: ESPNClient) -> None:
    console.print("[bold green]Fantasy GM supervisor started[/bold green]")

    summary, _ = load_league_summary(client)

    draft_time = datetime.fromtimestamp(
        summary.draft_date_ms / 1000,
        tz=timezone.utc,
    )

    eastern = ZoneInfo("America/New_York")

    console.print(
        "Draft scheduled for: "
        f"{draft_time.astimezone(eastern):%Y-%m-%d %I:%M %p %Z}"
    )

    draft_process: subprocess.Popen | None = None
    chatbot_process: subprocess.Popen | None = None

    # Start chat immediately so it can watch league activity well before
    # the draft room opens. Existing chat history is seeded as seen by the
    # transport, so this should only act on new events.
    if _chatbot_enabled():
        console.print(
            "[bold magenta]Launching league chatbot[/bold magenta]"
        )
        chatbot_process = _launch_chatbot_process()

    try:
        while True:
            try:
                draft_data, _ = load_draft_picks(client)

                drafted = bool(draft_data.get("drafted"))
                in_progress = bool(draft_data.get("inProgress"))

                # Reap the autonomous draft process if it exited.
                if draft_process is not None:
                    return_code = draft_process.poll()

                    if return_code is not None:
                        console.print(
                            f"[yellow]Draft process exited "
                            f"with code {return_code}[/yellow]"
                        )
                        draft_process = None

                # Reap the chatbot independently if it exited.
                if chatbot_process is not None:
                    return_code = chatbot_process.poll()

                    if return_code is not None:
                        console.print(
                            f"[yellow]Chatbot process exited "
                            f"with code {return_code}[/yellow]"
                        )
                        chatbot_process = None

                # Once ESPN declares the draft complete:
                # - stop chat
                # - do not relaunch either child
                # - keep the supervisor alive so Railway does not restart
                if drafted:
                    if chatbot_process is not None:
                        _stop_process(
                            chatbot_process,
                            name="league chatbot",
                        )
                        chatbot_process = None

                    time.sleep(POLL_SECONDS)
                    continue

                # Chatbot should stay alive any time the supervisor is active
                # before draft completion.
                if chatbot_process is None and _chatbot_enabled():
                    console.print(
                        "[bold magenta]Launching league chatbot[/bold magenta]"
                    )
                    chatbot_process = _launch_chatbot_process()

                now = datetime.now(timezone.utc)
                time_until_draft = draft_time - now

                should_launch = (
                    in_progress
                    or time_until_draft <= DRAFT_LEAD_TIME
                )

                # The autonomous draft manager still waits until the draft
                # is imminent or ESPN marks it in progress.
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

    finally:
        _stop_process(
            chatbot_process,
            name="league chatbot",
        )
        _stop_process(
            draft_process,
            name="autonomous draft manager",
        )
