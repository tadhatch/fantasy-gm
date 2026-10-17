from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fantasy_gm.espn.transactions import (
    ESPNTransactionsClient,
    TransactionsSettings,
)
from fantasy_gm.lineup.availability import Availability, AvailabilityStatus
from fantasy_gm.lineup.models import LineupCandidate
from fantasy_gm.models.roster import RosterEntry
from fantasy_gm.repair import service
from fantasy_gm.repair.engine import RepairSettings, plan_repair
from fantasy_gm.repair.models import FreeAgent, LeagueRosterRules, RosterAsset

QB, RB, WR, TE, FLEX, BENCH, IR = 0, 2, 4, 6, 23, 20, 21
POS = {QB: 1, RB: 2, WR: 3, TE: 4}
ELIGIBLE = {
    QB: [QB, BENCH, IR],
    RB: [RB, FLEX, BENCH, IR],
    WR: [WR, FLEX, BENCH, IR],
    TE: [TE, FLEX, BENCH, IR],
}
SLOTS = {QB: 1, RB: 1, WR: 1, TE: 1, FLEX: 1, BENCH: 3}  # roster max 8

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=2)

OK = Availability(AvailabilityStatus.ELIGIBLE, "healthy", "ACTIVE", "espn", NOW)


def out(status="OUT"):
    return Availability(
        AvailabilityStatus.INELIGIBLE, f"ESPN status {status}", status, "espn", NOW
    )


def asset(pid, pos, slot, *, avail=OK, ros=50.0, proj=10.0, locked=False,
          roster_locked=False, droppable=True):
    entry = RosterEntry(
        player_id=pid,
        name=f"P{pid}",
        lineup_slot_id=slot,
        eligible_slot_ids=ELIGIBLE[pos],
        default_position_id=POS[pos],
        pro_team_id=1,
        injury_status=avail.injury_status,
    )
    return RosterAsset(
        candidate=LineupCandidate(
            entry=entry,
            projected_points=proj,
            availability=avail,
            locked=locked,
            kickoff=KICKOFF,
        ),
        ros_value=ros,
        roster_locked=roster_locked,
        droppable=droppable,
    )


def fa(pid, pos, *, proj=12.0, ros=60.0, status="FREEAGENT", clears=None,
       avail=OK, kickoff=KICKOFF):
    return FreeAgent(
        player_id=pid,
        name=f"FA{pid}",
        position_id=POS[pos],
        eligible_slot_ids=ELIGIBLE[pos],
        pro_team_id=2,
        pool_status=status,
        availability=avail,
        weekly_projection=proj,
        ros_value=ros,
        kickoff=kickoff,
        waiver_clears_at=clears,
    )


def rules(**overrides):
    base = dict(
        slot_counts=SLOTS,
        position_limits={},
        roster_max=8,
        ir_eligible_statuses={"INJURY_RESERVE", "OUT"},
    )
    base.update(overrides)
    return LeagueRosterRules(**base)


def full_roster(*overrides):
    """8 players: a legal lineup plus 3 bench. Overrides replace by id."""
    roster = {
        1: asset(1, QB, QB, ros=200),
        2: asset(2, QB, BENCH, ros=120),
        10: asset(10, RB, RB, ros=150),
        11: asset(11, RB, FLEX, ros=90),
        20: asset(20, WR, WR, ros=140),
        21: asset(21, WR, BENCH, ros=30),
        30: asset(30, TE, TE, ros=80),
        31: asset(31, TE, BENCH, ros=25),
    }
    roster.update({a.player_id: a for a in overrides})
    return list(roster.values())


def run(roster, free_agents, *, settings=None, pending=None, **rule_overrides):
    return plan_repair(
        roster,
        free_agents,
        rules(**rule_overrides),
        now=NOW,
        settings=settings or RepairSettings(),
        pending_claim_slots=pending,
    )


# --- detection -------------------------------------------------------


def test_bench_replacement_available_means_no_repair():
    plan = run(full_roster(asset(1, QB, QB, avail=out())), [fa(100, QB)])
    assert plan.uncovered_slots == {}
    assert plan.actions == []


def test_flex_covered_by_bench_te_needs_no_repair():
    plan = run(
        full_roster(asset(11, RB, FLEX, avail=out())), [fa(100, RB)]
    )
    assert plan.uncovered_slots == {}


# --- all QBs / all WRs unavailable ----------------------------------


def test_all_qbs_unavailable_acquires_a_qb():
    roster = full_roster(
        asset(1, QB, QB, avail=out(), ros=200),
        asset(2, QB, BENCH, avail=out("INJURY_RESERVE"), ros=5),
    )
    plan = run(roster, [fa(100, QB, proj=15, ros=100), fa(101, QB, proj=9)])
    assert plan.uncovered_slots == {QB: 1}
    action = plan.actions[0]
    assert action.add.player_id == 100
    # The season-long IR QB (ros 5) is cut; the star QB (ros 200) who's
    # only out this week is kept.
    assert action.drop.player_id == 2


def test_all_wrs_unavailable_rb_cannot_fill_wr_slot():
    roster = full_roster(
        asset(20, WR, WR, avail=out(), ros=140),
        asset(21, WR, BENCH, avail=out(), ros=30),
    )
    plan = run(roster, [fa(100, WR, ros=40)])
    assert plan.uncovered_slots == {WR: 1}
    assert plan.actions[0].add.player_id == 100


# --- drop candidate comparisons -------------------------------------


def test_valuable_injured_player_is_preserved_cheaper_drop_elsewhere():
    # Only RB besides the flex is the star, out this week (ros 150). The
    # cheapest expendable player is a bench TE (ros 25).
    roster = full_roster(
        asset(10, RB, RB, avail=out(), ros=150),
        asset(11, RB, BENCH, avail=out(), ros=140),
        asset(21, WR, FLEX, ros=30),
    )
    plan = run(roster, [fa(100, RB, proj=11, ros=45)])
    assert plan.actions[0].drop.player_id == 31


def test_best_value_prefers_affected_position_within_margin():
    roster = full_roster(
        asset(20, WR, WR, avail=out(), ros=140),
        asset(21, WR, BENCH, avail=out(), ros=30),
        asset(31, TE, BENCH, ros=25),
    )
    plan = run(roster, [fa(100, WR, ros=40)])
    # WR P21 (30) is within the 10-pt margin of TE P31 (25): drop the WR.
    assert plan.actions[0].drop.player_id == 21


def test_lowest_ros_strategy_ignores_position():
    roster = full_roster(
        asset(20, WR, WR, avail=out(), ros=140),
        asset(21, WR, BENCH, avail=out(), ros=30),
        asset(31, TE, BENCH, ros=25),
    )
    plan = run(
        roster,
        [fa(100, WR, ros=40)],
        settings=RepairSettings(strategy="lowest_ros"),
    )
    assert plan.actions[0].drop.player_id == 31


def test_same_position_strategy_stays_at_position():
    roster = full_roster(
        asset(20, WR, WR, avail=out(), ros=140),
        asset(21, WR, BENCH, avail=out(), ros=80),
        asset(31, TE, BENCH, ros=5),
    )
    plan = run(
        roster,
        [fa(100, WR, ros=90)],
        settings=RepairSettings(strategy="same_position"),
    )
    assert plan.actions[0].drop.player_id == 21


def test_drop_that_costs_more_than_it_gains_is_rejected():
    # Every expendable player is worth far more ROS than the replacement.
    roster = [
        asset(1, QB, QB, avail=out(), ros=250),
        asset(2, QB, BENCH, avail=out(), ros=240),
        asset(10, RB, RB, ros=200),
        asset(11, RB, FLEX, ros=190),
        asset(20, WR, WR, ros=180),
        asset(21, WR, BENCH, ros=170),
        asset(30, TE, TE, ros=160),
        asset(31, TE, BENCH, ros=150),
    ]
    plan = run(roster, [fa(100, QB, proj=8, ros=20)])
    assert plan.actions == []
    assert plan.unrepairable[0].slot_id == QB
    options = plan.unrepairable[0].details[0]["drop_options"]
    assert any("costs more roster value" in (o["rejected_reason"] or "")
               for o in options)


def test_never_drops_the_only_te():
    # The lone TE has the lowest value on the roster, but cutting him
    # would leave nobody for the TE slot.
    roster = [a for a in full_roster(
        asset(20, WR, WR, avail=out(), ros=140),
        asset(21, WR, BENCH, avail=out(), ros=130),
        asset(30, TE, TE, ros=1),
    ) if a.player_id != 31]
    roster.append(asset(12, RB, BENCH, ros=45))
    plan = run(roster, [fa(100, WR, ros=40)])
    assert plan.actions[0].drop.player_id != 30
    rejected = {o["player_id"]: o["rejected_reason"]
                for o in plan.actions[0].drop_options}
    assert "too few rostered players" in rejected[30]


# --- roster room ------------------------------------------------------


def test_open_roster_spot_means_no_drop():
    roster = [a for a in full_roster(asset(1, QB, QB, avail=out()),
                                     asset(2, QB, BENCH, avail=out()))
              if a.player_id != 31]
    plan = run(roster, [fa(100, QB)])
    assert plan.actions[0].drop is None
    assert plan.actions[0].ir_player_id is None


def test_open_ir_slot_stashes_injured_player_instead_of_dropping():
    roster = full_roster(
        asset(1, QB, QB, avail=out(), ros=200),
        asset(2, QB, BENCH, avail=out("INJURY_RESERVE"), ros=5),
    )
    plan = run(roster, [fa(100, QB)], slot_counts={**SLOTS, IR: 1})
    action = plan.actions[0]
    assert action.drop is None
    assert action.ir_player_id == 1  # highest-value IR-eligible player


def test_locked_and_undroppable_players_are_never_dropped():
    roster = full_roster(
        asset(1, QB, QB, avail=out(), ros=200),
        asset(2, QB, BENCH, avail=out(), ros=1, roster_locked=True),
        asset(31, TE, BENCH, ros=2, droppable=False),
        asset(21, WR, BENCH, ros=3, locked=True),
    )
    plan = run(roster, [fa(100, QB, ros=150)])
    drop = plan.actions[0].drop
    assert drop.player_id not in {2, 31, 21}
    rejected = {o["player_id"]: o["rejected_reason"]
                for o in plan.actions[0].drop_options}
    assert "locked" in rejected[2]
    assert "undroppable" in rejected[31]
    assert "locked" in rejected[21]


def test_position_limit_respected():
    roster = full_roster(
        asset(1, QB, QB, avail=out()),
        asset(2, QB, BENCH, avail=out()),
    )
    plan = run(
        roster,
        [fa(100, QB)],
        position_limits={1: 2},  # max 2 QBs
    )
    # Must drop a QB to stay at 2.
    assert plan.actions[0].drop.position_id == 1


# --- free-agent pool --------------------------------------------------


def test_waiver_player_clearing_after_kickoff_is_excluded():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    late = fa(100, QB, proj=25, status="WAIVERS", clears=KICKOFF - timedelta(minutes=10))
    plan = run(roster, [late, fa(101, QB, proj=10)])
    assert plan.actions[0].add.player_id == 101


def test_waiver_player_clearing_in_time_can_be_claimed():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    early = fa(100, QB, proj=25, status="WAIVERS", clears=NOW)
    plan = run(roster, [early, fa(101, QB, proj=10)])
    assert plan.actions[0].add.player_id == 100
    assert plan.actions[0].add.on_waivers


def test_near_equal_free_agent_preferred_over_waiver_claim():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(
        roster,
        [fa(100, QB, proj=15, status="WAIVERS", clears=NOW), fa(101, QB, proj=14)],
    )
    assert plan.actions[0].add.player_id == 101


def test_unavailable_or_bye_free_agents_excluded():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(
        roster,
        [fa(100, QB, proj=30, avail=out()), fa(101, QB, proj=25, kickoff=None)],
    )
    assert plan.actions == []
    assert "no available free agent" in plan.unrepairable[0].reason


def test_free_agent_whose_game_started_is_excluded():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(roster, [fa(100, QB, kickoff=NOW - timedelta(minutes=1))])
    assert plan.actions == []


def test_no_replacement_reports_unrepairable():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(roster, [fa(100, WR)])
    assert plan.actions == []
    assert plan.unrepairable[0].slot_id == QB


# --- idempotency / limits --------------------------------------------


def test_pending_claim_covers_slot_without_reclaiming():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(roster, [fa(100, QB)], pending=[ELIGIBLE[QB]])
    assert plan.actions == []
    assert plan.awaiting == {QB: 1}


def test_acquisition_limit_blocks_repair():
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out()))
    plan = run(roster, [fa(100, QB)], acquisition_blocked="season limit reached")
    assert plan.actions == []
    assert plan.unrepairable[0].reason == "season limit reached"


def test_multiple_uncovered_slots_each_get_a_distinct_player():
    roster = full_roster(
        asset(1, QB, QB, avail=out()),
        asset(2, QB, BENCH, avail=out(), ros=1),
        asset(20, WR, WR, avail=out()),
        asset(21, WR, BENCH, avail=out(), ros=2),
    )
    plan = run(roster, [fa(100, QB), fa(200, WR)])
    adds = {a.add.player_id for a in plan.actions}
    drops = {a.drop.player_id for a in plan.actions}
    assert adds == {100, 200}
    assert len(drops) == 2


# --- service orchestration -------------------------------------------


class _Txn(ESPNTransactionsClient):
    def __init__(self):
        client = SimpleNamespace(
            settings=SimpleNamespace(espn_season=2026, espn_swid="{x}"),
            league_id=1,
        )
        super().__init__(client, settings=TransactionsSettings(mode="live"))
        self.posted = []

    def _post(self, body, *, confirm, label):
        if not confirm:
            return None
        self.posted.append(body)
        return {"ok": True}


def _patch_service(monkeypatch, roster, free_agents):
    candidates = {a.player_id: a.candidate for a in roster}
    monkeypatch.setattr(
        service, "load_lineup_candidates", lambda *a, **k: (candidates, SLOTS)
    )
    monkeypatch.setattr(
        service, "_league_rules", lambda *a, **k: (rules(), 10)
    )
    monkeypatch.setattr(service, "_roster_assets", lambda *a, **k: roster)
    monkeypatch.setattr(service, "_free_agents", lambda *a, **k: free_agents)
    monkeypatch.setattr(service, "_pending_claim_slots", lambda *a, **k: [])
    lineup_runs = []
    monkeypatch.setattr(
        service, "submit_lineup_safely",
        lambda *a, **k: lineup_runs.append(k["trigger"]),
    )

    class Store:
        records = []

        def record(self, **kw):
            self.records.append(kw)

    return lineup_runs, Store()


def _client():
    return SimpleNamespace(settings=SimpleNamespace(espn_season=2026))


def test_service_free_agent_add_reruns_lineup_gate(monkeypatch):
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out(), ros=1))
    lineup_runs, store = _patch_service(monkeypatch, roster, [fa(100, QB)])
    txn = _Txn()
    outcome = service.run_roster_repair(
        _client(), team_id=1, week=7, confirm=True, now_fn=lambda: NOW,
        txn=txn, store=store, settings=RepairSettings(),
    )
    assert outcome.results == {100: "submitted"}
    assert txn.posted[0]["type"] == "FREEAGENT"
    assert lineup_runs == ["post_roster_repair"]
    assert store.records[-1]["actions"][0]["result"] == "submitted"


def test_service_waiver_claim_is_pending_not_assumed_complete(monkeypatch):
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out(), ros=1))
    lineup_runs, store = _patch_service(
        monkeypatch, roster, [fa(100, QB, status="WAIVERS", clears=NOW)]
    )
    txn = _Txn()
    outcome = service.run_roster_repair(
        _client(), team_id=1, week=7, confirm=True, now_fn=lambda: NOW,
        txn=txn, store=store, settings=RepairSettings(),
    )
    assert outcome.results == {100: "claim_pending"}
    assert txn.posted[0]["type"] == "WAIVER"
    assert lineup_runs == []  # roster unchanged until the claim processes


def test_service_shadows_by_default(monkeypatch):
    monkeypatch.delenv("FANTASY_GM_REPAIR_CONFIRM", raising=False)
    roster = full_roster(asset(1, QB, QB, avail=out()), asset(2, QB, BENCH, avail=out(), ros=1))
    lineup_runs, store = _patch_service(monkeypatch, roster, [fa(100, QB)])
    txn = _Txn()
    outcome = service.run_roster_repair(
        _client(), team_id=1, week=7, now_fn=lambda: NOW,
        txn=txn, store=store, settings=RepairSettings(),
    )
    assert outcome.results == {100: "shadow"}
    assert txn.posted == []


def test_bye_week_does_not_zero_out_ros_value(monkeypatch):
    # A D/ST on bye: weekly projection 0, but 30 real points over 5 games.
    monkeypatch.setattr(
        service, "extract_dst_week_projection", lambda *a, **k: 0.0
    )
    monkeypatch.setattr(
        service, "extract_season_actual", lambda *a, **k: 30.0
    )
    weekly, ros = service._ros_value(
        {"player": {}}, position_id=16, week=6, season=2026, remaining_weeks=12
    )
    assert weekly == 0.0
    assert ros == 30.0 / 5 * 12
