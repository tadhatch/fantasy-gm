# fantasy_gm/workers/test_worker.py

from __future__ import annotations

import os
import time

from rich.console import Console


console = Console()


def run_test_worker(
    *,
    sleep_seconds: int,
    job_id: str,
) -> None:
    console.print(
        f"[bold cyan]Disposable worker started[/bold cyan] "
        f"job={job_id} pid={os.getpid()}"
    )

    console.print(
        f"Worker will run for {sleep_seconds} seconds"
    )

    time.sleep(sleep_seconds)

    console.print(
        f"[bold green]Disposable worker complete[/bold green] "
        f"job={job_id}"
    )