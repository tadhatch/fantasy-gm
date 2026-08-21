from fantasy_gm.valuation.engine import ValuationEngine
from fantasy_gm.valuation.models import (
    AdjustmentCategory,
    ContextAdjustment,
    PlayerMetrics,
    PlayerRelationship,
)
from fantasy_gm.valuation.propagation import RelationshipPropagator


def test_context_adjustment_changes_value():
    engine = ValuationEngine()
    p = PlayerMetrics(
        espn_id=1,
        name="RB One",
        position="RB",
        projected_points=250,
        replacement_points=150,
        snap_share=.75,
        rush_share=.65,
        red_zone_share=.7,
        injury_risk=.1,
    )

    base = engine.value_player(p)
    injured = engine.value_player(
        p,
        adjustments=[
            ContextAdjustment(
                player_id=1,
                category=AdjustmentCategory.INJURY,
                delta=-12,
                confidence=.9,
                reason="Expected missed time",
            )
        ],
    )

    assert injured.intrinsic_value < base.intrinsic_value


def test_injury_propagates_to_backup():
    source = ContextAdjustment(
        player_id=1,
        category=AdjustmentCategory.INJURY,
        delta=-12,
        confidence=.9,
        reason="Starter expected to miss time",
    )
    relationships = [
        PlayerRelationship(
            source_player_id=1,
            target_player_id=2,
            relationship="direct_backup",
            strength=.9,
            target_position="RB",
        )
    ]

    out = RelationshipPropagator().propagate(source, relationships)
    assert out
    assert out[0].player_id == 2
    assert out[0].delta > 0
