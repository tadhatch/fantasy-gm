from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(slots=True)
class ReplacementLevels:
    points_by_position: dict[str, float]
    rank_by_position: dict[str, int]


# For Fantasy 8610:
#   14 teams
#   QB 1, RB 2, WR 3, TE 1, FLEX 1, bench 5
#
# The bench component is intentionally conservative for this first pass.
# Tomorrow we can derive it iteratively from simulated draft behavior.
DEFAULT_REPLACEMENT_RANKS = {
    "QB": 18,
    "RB": 45,
    "WR": 62,
    "TE": 18,
    "K": 14,
    "DST": 14,
}


def calculate_replacement_levels(
    projected_points: list[tuple[str, float]],
    replacement_ranks: dict[str, int] | None = None,
) -> ReplacementLevels:
    ranks = replacement_ranks or DEFAULT_REPLACEMENT_RANKS
    grouped: dict[str, list[float]] = defaultdict(list)

    for position, points in projected_points:
        if position:
            grouped[position].append(float(points))

    levels: dict[str, float] = {}
    for pos, values in grouped.items():
        values.sort(reverse=True)
        target_rank = ranks.get(pos, max(1, len(values) // 2))
        idx = min(len(values) - 1, max(0, target_rank - 1))
        levels[pos] = values[idx] if values else 0.0

    return ReplacementLevels(
        points_by_position=levels,
        rank_by_position=dict(ranks),
    )
