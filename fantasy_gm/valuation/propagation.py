from __future__ import annotations

from collections import defaultdict

from .models import AdjustmentCategory, ContextAdjustment, Evidence, PlayerRelationship


class RelationshipPropagator:
    """
    Propagates player events through a roster/depth-chart relationship graph.

    Example:
      RB1 injury -> RB2/RB3 opportunity increase
      QB injury  -> WR/TE team-environment decrease
      LT injury  -> QB/RB small offensive-line decrease
    """

    def propagate(
        self,
        source_adjustment: ContextAdjustment,
        relationships: list[PlayerRelationship],
    ) -> list[ContextAdjustment]:
        results: list[ContextAdjustment] = []

        for rel in relationships:
            if rel.source_player_id != source_adjustment.player_id:
                continue

            multiplier = self._multiplier(
                source_adjustment.category,
                rel.relationship,
                rel.target_position,
            )
            if multiplier == 0:
                continue

            delta = abs(source_adjustment.delta) * rel.strength * multiplier

            results.append(
                ContextAdjustment(
                    player_id=rel.target_player_id,
                    category=AdjustmentCategory.OPPORTUNITY,
                    delta=delta,
                    confidence=source_adjustment.confidence * rel.strength,
                    reason=(
                        f"Propagated from player {source_adjustment.player_id}: "
                        f"{source_adjustment.reason} via {rel.relationship}"
                    ),
                    evidence=source_adjustment.evidence,
                    expires_at=source_adjustment.expires_at,
                )
            )

        return results

    def _multiplier(
        self,
        category: AdjustmentCategory,
        relationship: str,
        target_position: str | None,
    ) -> float:
        # Positive multipliers mean "source bad news creates target opportunity".
        if category in {AdjustmentCategory.INJURY, AdjustmentCategory.SUSPENSION}:
            if relationship in {"direct_backup", "depth_chart_competitor"}:
                return 0.65
            if relationship == "same_backfield":
                return 0.35
            if relationship == "target_competitor":
                return 0.25
            if relationship == "same_offense" and target_position in {"WR", "TE"}:
                return -0.10

        if category == AdjustmentCategory.OFFENSIVE_LINE:
            if relationship == "same_offense":
                return -0.15

        if category == AdjustmentCategory.COACHING:
            if relationship == "same_offense":
                return 0.10

        return 0.0
