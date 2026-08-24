from __future__ import annotations

import os
import subprocess
import sys
import time

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.league import load_league_summary


POLL_SECONDS = 60

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
    console.print(
        "[bold green]Fantasy GM supervisor started in GM mode[/bold green]"
    )

    summary, _ = load_league_summary(client)

    console.print(
        f"League: [bold]{summary.name}[/bold] "
        f"| Team: {summary.team_name} "
        f"| Team ID: {summary.team_id}"
    )

    chatbot_process: subprocess.Popen | None = None

    # Temporary behavior:
    # keep the chatbot alive as a local child process.
    # Next step will move it into its own Railway service.
    if _chatbot_enabled():
        console.print(
            "[bold magenta]Launching league chatbot[/bold magenta]"
        )
        chatbot_process = _launch_chatbot_process()

    try:
        while True:
            try:
                if chatbot_process is not None:
                    return_code = chatbot_process.poll()

                    if return_code is not None:
                        console.print(
                            f"[yellow]Chatbot process exited "
                            f"with code {return_code}[/yellow]"
                        )
                        chatbot_process = None

                if chatbot_process is None and _chatbot_enabled():
                    console.print(
                        "[bold magenta]Restarting league chatbot[/bold magenta]"
                    )
                    chatbot_process = _launch_chatbot_process()

                # GM event detection / Railway worker orchestration
                # will be added here next.

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
