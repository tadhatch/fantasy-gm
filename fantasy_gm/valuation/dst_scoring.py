from __future__ import annotations

"""
ESPN never populates `appliedTotal` for D/ST entries -- confirmed
empirically across a full season of real games (every appliedTotal for a
D/ST is 0.0, both projected and actual). The underlying per-category raw
stat values ARE present, and their statId identities were verified by
correlating a full 2025 season of every team's real per-game raw stat
blob against nflreadpy's independently-sourced actual defensive box
scores (sacks, interceptions, points allowed, etc.) -- each statId below
matched its real-world counterpart at 85-100% exact agreement across 544
(team, week) samples (some mismatch is expected: e.g. split sacks can
round differently between sources).

The per-event POINT VALUES and the points-allowed tier cutoffs below are
ESPN's well-known default D/ST scoring table, not this league's verified
settings -- this league hasn't customized D/ST scoring away from default,
so there's no explicit table in the API to confirm the point values
against (only the raw stat identities, which are verified). Good enough
for a lineup start/sit signal; treat the point values as an assumption,
not a confirmed fact. Blocked kicks are deliberately left out: no statId
correlated consistently with them in the verification data, so guessing
one would be worse than omitting the category.
"""

# statId -> ESPN's default point value per occurrence.
EVENT_POINTS: dict[int, float] = {
    99: 1.0,  # sacks
    95: 2.0,  # interceptions
    96: 2.0,  # fumble recoveries (of the opponent)
    98: 2.0,  # safeties
    103: 6.0,  # defensive/return touchdowns
}

POINTS_ALLOWED_STAT_ID = 120

# (max points allowed inclusive, fantasy points) -- ESPN's default tiers.
POINTS_ALLOWED_BRACKETS: list[tuple[float, float]] = [
    (0, 10.0),
    (6, 7.0),
    (13, 4.0),
    (17, 1.0),
    (21, 0.0),
    (27, -1.0),
    (34, -4.0),
    (45, -7.0),
    (float("inf"), -10.0),
]


def _points_allowed_score(points_allowed: float) -> float:
    for cutoff, points in POINTS_ALLOWED_BRACKETS:
        if points_allowed <= cutoff:
            return points
    return POINTS_ALLOWED_BRACKETS[-1][1]


def estimate_dst_points(raw_stats: dict[str, float]) -> float | None:
    """
    Reconstruct a D/ST's fantasy points from its raw per-category stat
    values, since ESPN's own appliedTotal is always 0 for this position.
    Works for both a projection row (fractional expected-value stats) and
    an actual row (real integer counts) -- same categories, same math.
    Returns None if the row has no usable data at all.
    """
    if not raw_stats:
        return None

    total = 0.0
    for stat_id, points in EVENT_POINTS.items():
        value = raw_stats.get(str(stat_id))
        if value:
            total += value * points

    points_allowed = raw_stats.get(str(POINTS_ALLOWED_STAT_ID))
    if points_allowed is not None:
        total += _points_allowed_score(points_allowed)

    return total
