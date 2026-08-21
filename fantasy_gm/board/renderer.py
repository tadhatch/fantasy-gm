from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .models import BoardPlayer


def render_board(rows: list[BoardPlayer], *, limit: int = 50) -> None:
    console = Console()

    table = Table(title=f"Fantasy GM Draft Board — top {limit}")
    table.add_column("RK", justify="right")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("Proj", justify="right")
    table.add_column("VOR", justify="right")
    table.add_column("Snap", justify="right")
    table.add_column("Tgt/Rush", justify="right")
    table.add_column("ECR", justify="right")
    table.add_column("ADP", justify="right")
    table.add_column("Next%", justify="right")
    table.add_column("Fan", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Pick", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Flags")

    for rank, row in enumerate(rows[:limit], start=1):
        share = "-"
        if row.position in {"WR", "TE"} and row.target_share is not None:
            share = f"{row.target_share:.0%}"
        elif row.position in {"RB", "QB"} and row.rush_share is not None:
            share = f"{row.rush_share:.0%}"

        table.add_row(
            str(rank),
            row.name,
            row.position,
            f"{row.projected_points:.1f}",
            f"{row.vor:+.1f}",
            f"{row.snap_share:.0%}" if row.snap_share is not None else "-",
            share,
            f"{row.ecr:.1f}" if row.ecr is not None else "-",
            f"{row.adp:.1f}" if row.adp is not None else "-",
            f"{row.next_pick_survival:.0%}" if row.next_pick_survival is not None else "-",
            f"{row.fandom_bonus:+.1f}" if row.fandom_bonus else "-",
            f"{row.board_score:.2f}",
            f"{row.pick_value:.2f}",
            f"{row.confidence:.0%}",
            ",".join(row.flags) if row.flags else "-",
        )

    console.print(table)
    console.print(
        "\n[dim]Value = intrinsic value + market sanity check + current depth role. "
        "Pick = Value + urgency based on estimated survival to our next selection. "
        "Next% is intentionally a heuristic until calibrated.[/dim]"
    )
