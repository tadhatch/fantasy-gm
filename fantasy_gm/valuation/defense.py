from __future__ import annotations
from fantasy_gm.defense.models import DefenseStats, DefenseValuation, DefenseContextEffect
from fantasy_gm.defense.context import aggregate_defense_context

def _football_score(stats: DefenseStats) -> float:
    score = 0.0
    score += stats.sacks_per_game * 2.8
    score += stats.pressure_rate * 26.0
    score += stats.takeaways_per_game * 3.8
    score += stats.defensive_tds_per_game * 8.0
    score += max(-5.0, min(7.0, (24.0 - stats.points_allowed_per_game) * 0.55))
    score += max(-3.0, min(4.0, (350.0 - stats.yards_allowed_per_game) * 0.025))
    if stats.red_zone_td_rate is not None:
        score += max(-2.0, min(2.0, (0.58 - stats.red_zone_td_rate) * 10.0))
    if stats.explosive_play_rate is not None:
        score += max(-2.0, min(2.0, (0.10 - stats.explosive_play_rate) * 20.0))
    return max(0.0, score)

def _schedule_score(stats: DefenseStats) -> float:
    return stats.early_schedule_score * 3.0 + stats.season_schedule_score * 1.25

def value_defense(
    stats: DefenseStats,
    *,
    context_effects: list[DefenseContextEffect] | None = None,
) -> DefenseValuation:
    effects = context_effects or []
    football = _football_score(stats)
    schedule = _schedule_score(stats)
    context, effects = aggregate_defense_context(effects)
    projected_points = max(0.0, 85.0 + football * 2.1 + schedule * 1.5 + context * 2.0)
    board_score = max(0.0, football + schedule + context)

    flags = []
    if context <= -2.0: flags.append(f"CTX{context:+.1f}")
    if stats.early_schedule_score >= 1.0: flags.append("EARLY_SCHED+")
    elif stats.early_schedule_score <= -1.0: flags.append("EARLY_SCHED-")

    return DefenseValuation(
        team_id=stats.team_id,
        team=stats.team,
        football_score=football,
        schedule_score=schedule,
        context_score=context,
        projected_points=projected_points,
        board_score=board_score,
        confidence=0.78 if effects else 0.72,
        effects=effects,
        flags=flags,
    )
