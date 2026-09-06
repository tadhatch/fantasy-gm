from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .postgres_store import PostgresContextStore


def render_context(limit: int | None = None) -> None:
    console = Console()
    rows = list(PostgresContextStore().load_all().values())
    rows.sort(key=lambda r: abs(r.weighted_delta()), reverse=True)
    if limit is not None:
        rows = rows[:limit]

    table = Table(title="Fantasy GM AI Context")
    table.add_column("Player")
    table.add_column("Cat")
    table.add_column("Raw", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Applied", justify="right")
    table.add_column("Avail", justify="right")
    table.add_column("Distr", justify="right")
    table.add_column("Summary")

    for row in rows:
        table.add_row(
            row.player_name,
            row.category,
            f"{row.direct_delta:+.1f}",
            f"{row.confidence:.0%}",
            f"{row.weighted_delta():+.1f}",
            f"{row.availability_risk:.0%}",
            f"{row.distraction_risk:.0%}",
            row.summary[:100],
        )

    console.print(table)
