from __future__ import annotations

import math
from typing import Any

from fantasy_gm.data.depth import build_depth_index, opportunity_adjustment
from fantasy_gm.data.performance import build_performance_index
from fantasy_gm.data.player_mapper import build_market_index
from fantasy_gm.espn.constants import POSITION_IDS
from fantasy_gm.valuation.engine import ValuationEngine
from fantasy_gm.valuation.models import PlayerMetrics
from fantasy_gm.valuation.projections import extract_season_projection
from fantasy_gm.valuation.replacement import calculate_replacement_levels
from fantasy_gm.valuation.survival import survival_probability, urgency_bonus

from .models import BoardPlayer


DRAFTABLE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}


def build_board(
    player_pool: list[dict[str, Any]],
    *,
    season: int,
    next_pick: int | None = 15,
    history_season: int | None = None,
    favorite_team: str | None = None,
    fandom_weight: float = 1.5,
) -> list[BoardPlayer]:
    market = build_market_index()
    performance = build_performance_index(history_season or (season - 1))
    depth = build_depth_index(season)
    engine = ValuationEngine()

    from fantasy_gm.fandom.service import FandomService
    fandom = (FandomService(favorite_team, current_season=season,
    max_bonus=fandom_weight).build_index() if favorite_team else {})

    candidates: list[dict[str, Any]] = []

    for entry in player_pool:
        player, _ = _pool_parts(entry)
        pos_id = player.get("defaultPositionId")
        position = POSITION_IDS.get(pos_id, str(pos_id or "?"))
        if position not in DRAFTABLE_POSITIONS:
            continue

        projection = extract_season_projection(entry, season)
        if projection is None or projection.projected_points <= 0:
            continue

        owned, adp = _ownership(entry)
        espn_id = int(entry.get("id") or player.get("id"))

        candidates.append(
            {
                "player": player,
                "espn_id": espn_id,
                "position": position,
                "projection": projection.projected_points,
                "owned": owned,
                "adp": adp,
            }
        )

    replacement = calculate_replacement_levels(
        [(c["position"], c["projection"]) for c in candidates]
    )

    board: list[BoardPlayer] = []

    for c in candidates:
        player = c["player"]
        espn_id = c["espn_id"]
        position = c["position"]
        projected = c["projection"]
        replacement_points = replacement.points_by_position.get(position, 0.0)
        vor = projected - replacement_points

        market_row = market.get(espn_id)
        perf = performance.get(espn_id)
        depth_info = depth.get(espn_id)

        ecr = market_row.ecr if market_row else None
        ecr_sd = market_row.ecr_sd if market_row else None

        injury_status = player.get("injuryStatus")

        metrics = PlayerMetrics(
            espn_id=espn_id,
            name=player.get("fullName") or f"ESPN {espn_id}",
            position=position,
            nfl_team=player.get("proTeamId"),
            projected_points=projected,
            replacement_points=replacement_points,
            snap_share=perf.snap_share if perf else None,
            target_share=perf.target_share if perf else None,
            rush_share=perf.rush_share if perf else None,
            red_zone_share=perf.red_zone_share if perf else None,
            yards_per_route_run=perf.yards_per_route_run if perf else None,
            rush_yards_over_expected=perf.rush_yards_over_expected if perf else None,
            receiving_epa_per_target=perf.receiving_epa_per_target if perf else None,
            injury_risk=_injury_risk(injury_status),
            adp=c["adp"],
            espn_ownership_pct=c["owned"],
            metadata={"ecr": ecr, "ecr_sd": ecr_sd},
        )

        valuation = engine.value_player(metrics)
        market_score = _market_score(ecr, ecr_sd)
        depth_score, depth_flag = opportunity_adjustment(depth_info)

        survival = survival_probability(
            next_pick=next_pick,
            adp=c["adp"],
            ecr=ecr,
            ecr_sd=ecr_sd,
        )

        board_score = valuation.intrinsic_value + market_score + depth_score
        pick_value = board_score + urgency_bonus(survival)

        flags: list[str] = []
        if injury_status and injury_status.upper() not in {"ACTIVE", "NORMAL"}:
            flags.append(injury_status)
        if depth_flag:
            flags.append(depth_flag)
        if ecr is None:
            flags.append("NO_ECR")
        if c["adp"] is None:
            flags.append("NO_ADP")
        if perf is None:
            flags.append("NO_USAGE")

        confidence = valuation.confidence
        if perf is not None:
            confidence = min(0.99, confidence + 0.10)
        if ecr is not None:
            confidence = min(0.99, confidence + 0.05)
        if depth_info is not None:
            confidence = min(0.99, confidence + 0.05)
        if ecr_sd is not None and ecr_sd > 15:
            confidence = max(0.25, confidence - 0.08)

        fan = fandom.get(espn_id)
        fandom_bonus = fan.bonus if fan else 0.0
        board_score += fandom_bonus
        pick_value += fandom_bonus
        if fandom_bonus >= 0.1:
            flags.append(f"FAN+{fandom_bonus:.1f}" + ("*" if fan and fan.current_player else ""))

        board.append(
            BoardPlayer(
                espn_id=espn_id,
                name=metrics.name,
                position=position,
                nfl_team_id=player.get("proTeamId"),
                injury_status=injury_status,
                projected_points=projected,
                replacement_points=replacement_points,
                vor=vor,
                ecr=ecr,
                ecr_sd=ecr_sd,
                adp=c["adp"],
                fandom_bonus=fandom_bonus,
                fandom_relationship=fan.relationship if fan else None,         
                percent_owned=c["owned"],
                snap_share=perf.snap_share if perf else None,
                target_share=perf.target_share if perf else None,
                rush_share=perf.rush_share if perf else None,
                depth_rank=depth_info.depth_rank if depth_info else None,
                next_pick_survival=survival,
                valuation=valuation,
                market_score=market_score,
                depth_score=depth_score,
                board_score=board_score,
                pick_value=pick_value,
                confidence=confidence,
                flags=flags,
            )
        )

    from fantasy_gm.context.service import resolve_context

    resolved = resolve_context(board)

    for row in board:
        direct = resolved.direct.get(row.espn_id)
        direct_delta = direct.weighted_delta() if direct else 0.0
        propagated = resolved.propagated_delta.get(row.espn_id, 0.0)

        ai_total = max(-20.0, min(20.0, direct_delta + propagated))

        row.board_score += ai_total
        row.pick_value += ai_total

        if direct and abs(direct_delta) >= 0.5:
            row.flags.append(f"AI{direct_delta:+.1f}")

        if abs(propagated) >= 0.5:
            row.flags.append(f"PROP{propagated:+.1f}")

        if direct:
            # AI research is additional current evidence, but do not let it
            # produce fake certainty.
            row.confidence = min(
                0.99,
                max(row.confidence, 0.55 + 0.35 * direct.confidence),
            )

    board.sort(key=lambda p: p.pick_value, reverse=True)
    return board


def _pool_parts(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return entry.get("player") or entry, entry.get("playerPoolEntry") or entry


def _ownership(entry: dict[str, Any]) -> tuple[float | None, float | None]:
    player, pool = _pool_parts(entry)
    ownership = pool.get("ownership") or player.get("ownership") or entry.get("ownership") or {}

    owned = (
        ownership.get("percentOwned")
        if ownership.get("percentOwned") is not None
        else pool.get("percentOwned")
    )
    adp = ownership.get("averageDraftPosition")
    return _float(owned), _float(adp)


def _market_score(ecr: float | None, sd: float | None) -> float:
    if ecr is None:
        return 0.0
    score = max(-2.0, 8.0 - math.log1p(max(0.0, ecr - 1.0)) * 2.0)
    if sd is not None:
        score -= min(2.0, max(0.0, sd - 8.0) / 8.0)
    return score


def _injury_risk(status: str | None) -> float:
    if not status:
        return 0.0
    return {
        "ACTIVE": 0.0,
        "QUESTIONABLE": 0.10,
        "DOUBTFUL": 0.35,
        "OUT": 0.70,
        "INJURY_RESERVE": 0.85,
        "SUSPENDED": 0.75,
    }.get(status.upper(), 0.05)


def _float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
