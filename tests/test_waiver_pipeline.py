from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

from fantasy_gm.models.roster import TeamRoster
from fantasy_gm.waiver.models import GMDecision
from fantasy_gm.waiver.pipeline import _gate_transaction, _run_waiver_pipeline_body


def _decision(**overrides) -> GMDecision:
    base = GMDecision(
        action="add_drop",
        add_player_id=101,
        add_player_name="Add Candidate",
        drop_player_id=201,
        drop_player_name="Drop Candidate",
        material_upgrade=True,
        confidence=0.8,
        current_week_delta=1.0,
        next_4_weeks_delta=1.0,
        ros_value_delta=3.0,
        addresses_roster_need=False,
        reasoning="test",
    )
    return replace(base, **overrides)


# --- Pure gate tests -------------------------------------------------


def test_scenario_1_current_week_gain_but_worse_ros_is_no_move():
    decision = _decision(
        current_week_delta=1.4,
        next_4_weeks_delta=0.6,
        ros_value_delta=-2.1,
    )
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=False
    )
    assert allowed is False
    assert category == "drop_cost_too_high"


def test_scenario_2_negligible_bench_swap_is_no_move():
    decision = _decision(material_upgrade=False)
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=False
    )
    assert allowed is False
    assert category == "not_material"


def test_scenario_3_injury_created_starter_can_be_added():
    decision = _decision(
        confidence=0.75,
        current_week_delta=2.0,
        next_4_weeks_delta=3.5,
        ros_value_delta=4.0,
    )
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=True
    )
    assert allowed is True
    assert category is None


def test_scenario_4_dropping_valuable_ros_player_is_no_move():
    decision = _decision(
        current_week_delta=3.0,
        next_4_weeks_delta=2.0,
        ros_value_delta=-1.5,
    )
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=False
    )
    assert allowed is False
    assert category == "drop_cost_too_high"


def test_scenario_5_replacing_expendable_bench_player_is_add():
    decision = _decision(ros_value_delta=3.0)
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=False
    )
    assert allowed is True
    assert category is None


def test_scenario_6_real_short_term_need_allows_streamer():
    decision = _decision(
        addresses_roster_need=True,
        confidence=0.7,
        current_week_delta=2.0,
        next_4_weeks_delta=1.0,
        ros_value_delta=-1.0,
    )
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=False
    )
    assert allowed is True
    assert category is None


def test_scenario_7_high_acquisition_cost_for_marginal_gain_is_no_move():
    decision = _decision(confidence=0.6, ros_value_delta=1.0)
    allowed, category = _gate_transaction(
        decision, spends_acquisition_cost=True
    )
    assert allowed is False
    assert category == "acquisition_cost_too_high"


# --- Pipeline-level integration tests ---------------------------------


def _run_pipeline(stage_responses):
    client = MagicMock()
    client.get_player_pool.return_value = {"players": []}
    client.get_league.return_value = {
        "settings": {"acquisitionSettings": {"isUsingAcquisitionBudget": False}}
    }

    roster = TeamRoster(team_id=5, team_name="Test Team", entries=[])
    store = MagicMock()
    store.load_all.return_value = {}
    router = MagicMock()

    txn_instance = MagicMock()
    txn_instance.add_drop.return_value = {"id": "txn-1"}

    with patch(
        "fantasy_gm.waiver.pipeline.call_structured_json",
        side_effect=stage_responses,
    ), patch(
        "fantasy_gm.waiver.pipeline.load_team_roster", return_value=roster
    ), patch(
        "fantasy_gm.waiver.pipeline.current_scoring_period", return_value=1
    ), patch(
        "fantasy_gm.waiver.pipeline.ESPNTransactionsClient",
        return_value=txn_instance,
    ) as txn_class:
        result = _run_waiver_pipeline_body(
            client,
            team_id=5,
            free_agent_pool_size=10,
            shortlist_size=5,
            deep_dive_budget=0,
            attempt_transaction=True,
            confirm=True,
            store=store,
            router=router,
            reasoning_model="test-model",
            gm_model="test-model",
        )

    return result, txn_class, txn_instance


_STAGE1 = {
    "strengths": [],
    "weaknesses": [],
    "positional_needs": [],
    "expendable_players": [],
    "summary": "",
}
_STAGE2_EMPTY = {"shortlist": []}
_STAGE3_EMPTY = {"candidate_moves": [], "deep_dive_requests": []}


def test_scenario_8_no_compelling_moves_completes_without_transaction():
    stage_responses = [
        _STAGE1,
        _STAGE2_EMPTY,
        _STAGE3_EMPTY,
        {"action": "no_move", "reasoning": "nothing worth doing"},
    ]

    result, txn_class, txn_instance = _run_pipeline(stage_responses)

    assert result.decision.action == "no_move"
    assert result.executed is False
    txn_class.assert_not_called()
    txn_instance.add_drop.assert_not_called()


def test_scenario_9_no_move_cannot_reach_transaction_path():
    stage_responses = [
        _STAGE1,
        _STAGE2_EMPTY,
        _STAGE3_EMPTY,
        {
            "action": "add_drop",
            "add_player_id": 101,
            "add_player_name": "Overreaching Add",
            "drop_player_id": 201,
            "drop_player_name": "Drop Candidate",
            "material_upgrade": False,
            "confidence": 0.9,
            "current_week_delta": 3.0,
            "next_4_weeks_delta": 2.0,
            "ros_value_delta": 5.0,
            "reasoning": "LLM thinks this is great",
        },
    ]

    result, txn_class, txn_instance = _run_pipeline(stage_responses)

    assert result.decision.action == "no_move"
    assert result.executed is False
    txn_instance.add_drop.assert_not_called()


def test_material_upgrade_executes_transaction():
    stage_responses = [
        _STAGE1,
        _STAGE2_EMPTY,
        _STAGE3_EMPTY,
        {
            "action": "add_drop",
            "add_player_id": 101,
            "add_player_name": "Real Upgrade",
            "drop_player_id": 201,
            "drop_player_name": "Expendable Bench Player",
            "material_upgrade": True,
            "confidence": 0.85,
            "current_week_delta": 2.0,
            "next_4_weeks_delta": 3.0,
            "ros_value_delta": 4.0,
            "reasoning": "Clear rest-of-season upgrade",
        },
    ]

    result, txn_class, txn_instance = _run_pipeline(stage_responses)

    assert result.decision.action == "add_drop"
    assert result.executed is True
    txn_instance.add_drop.assert_called_once()
