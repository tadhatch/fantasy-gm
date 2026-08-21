from __future__ import annotations

from dataclasses import replace
from statistics import mean

from .models import ContextAdjustment, PlayerMetrics, PlayerValuation


POSITION_SCARCITY_WEIGHTS = {
    "RB": 1.15,
    "WR": 1.10,
    "TE": 1.00,
    "QB": 0.85,
    "K": 0.35,
    "DST": 0.35,
}


class ValuationEngine:
    """
    First-pass valuation engine.

    Important design goal:
    deterministic quantitative scoring first, bounded contextual/AI adjustments second.
    """

    def value_player(
        self,
        metrics: PlayerMetrics,
        *,
        adjustments: list[ContextAdjustment] | None = None,
        survival_probability: float | None = None,
    ) -> PlayerValuation:
        adjustments = adjustments or []

        projection = self._projection_score(metrics)
        replacement = self._replacement_score(metrics)
        usage = self._usage_score(metrics)
        efficiency = self._efficiency_score(metrics)
        risk = self._risk_score(metrics)
        context = sum(adj.bounded_delta() for adj in adjustments)
        scarcity = self._scarcity_score(metrics)

        intrinsic = (
            projection
            + replacement
            + usage
            + efficiency
            + scarcity
            + context
            + risk
        )

        pick_value = intrinsic
        if survival_probability is not None:
            # If a player is unlikely to survive until our next selection,
            # modestly increase the urgency of taking him now.
            urgency = max(0.0, min(1.0, 1.0 - survival_probability))
            pick_value += 5.0 * urgency

        confidence = self._confidence(metrics, adjustments)

        return PlayerValuation(
            player_id=metrics.espn_id,
            name=metrics.name,
            position=metrics.position,
            projection_score=projection,
            replacement_score=replacement,
            usage_score=usage,
            efficiency_score=efficiency,
            risk_score=risk,
            context_score=context,
            scarcity_score=scarcity,
            intrinsic_value=intrinsic,
            pick_value=pick_value,
            confidence=confidence,
            adjustments=adjustments,
        )

    def _projection_score(self, p: PlayerMetrics) -> float:
        # Normalize season projection into a useful range.
        return p.projected_points / 8.0

    def _replacement_score(self, p: PlayerMetrics) -> float:
        vor = max(0.0, p.projected_points - p.replacement_points)
        return (vor / 8.0) * POSITION_SCARCITY_WEIGHTS.get(p.position, 1.0)

    def _usage_score(self, p: PlayerMetrics) -> float:
        factors = [
            p.snap_share,
            p.route_participation,
            p.target_share,
            p.rush_share,
            p.red_zone_share,
        ]
        vals = [v for v in factors if v is not None]
        if not vals:
            return 0.0
        return mean(vals) * 12.0

    def _efficiency_score(self, p: PlayerMetrics) -> float:
        score = 0.0
        if p.yards_per_route_run is not None:
            score += min(4.0, max(-2.0, (p.yards_per_route_run - 1.5) * 2.0))
        if p.rush_yards_over_expected is not None:
            score += min(4.0, max(-2.0, p.rush_yards_over_expected))
        if p.receiving_epa_per_target is not None:
            score += min(4.0, max(-2.0, p.receiving_epa_per_target * 8.0))
        return score

    def _risk_score(self, p: PlayerMetrics) -> float:
        injury_penalty = -12.0 * max(0.0, min(1.0, p.injury_risk))
        volatility_penalty = -6.0 * max(0.0, min(1.0, p.role_volatility))
        return injury_penalty + volatility_penalty

    def _scarcity_score(self, p: PlayerMetrics) -> float:
        return {
            "RB": 4.0,
            "WR": 3.0,
            "TE": 1.5,
            "QB": 0.5,
            "K": -4.0,
            "DST": -4.0,
        }.get(p.position, 0.0)

    def _confidence(
        self, p: PlayerMetrics, adjustments: list[ContextAdjustment]
    ) -> float:
        known = [
            p.projected_points > 0,
            p.replacement_points >= 0,
            p.snap_share is not None,
            p.target_share is not None or p.rush_share is not None,
            p.adp is not None,
        ]
        base = sum(known) / len(known)
        if adjustments:
            base = (base + mean(a.confidence for a in adjustments)) / 2
        return round(max(0.25, min(0.99, base)), 3)
