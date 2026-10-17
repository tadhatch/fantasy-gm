from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fantasy_gm.espn.transactions import (
    ESPNTransactionsClient,
    LineupMove,
    TransactionsSettings,
    UnvalidatedLineupError,
)
from fantasy_gm.lineup import service
from fantasy_gm.lineup.validator import LineupValidation, validate_assignments
from fantasy_gm.models.roster import RosterEntry, TeamRoster

QB, RB, WR, FLEX, BENCH, IR = 0, 2, 4, 23, 20, 21
SLOTS = {QB: 1, RB: 1, WR: 1, FLEX: 1, BENCH: 5}
ELIGIBLE = {
    QB: [QB, BENCH, IR],
    RB: [RB, FLEX, BENCH, IR],
    WR: [WR, FLEX, BENCH, IR],
}
POSITION_ID = {QB: 1, RB: 2, WR: 3}

KICKOFF = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)
PRE_INACTIVES = KICKOFF - timedelta(hours=2)
POST_INACTIVES = KICKOFF - timedelta(minutes=60)

ON_BYE_TEAM = 99
UNKNOWN_SCHEDULE_TEAM = 98
LATE_GAME_TEAM = 97


def player(pid, pos, slot, status="ACTIVE", *, team=1, locked=None):
    return RosterEntry(
        player_id=pid,
        name=f"P{pid}",
        lineup_slot_id=slot,
        eligible_slot_ids=ELIGIBLE[pos],
        default_position_id=POSITION_ID[pos],
        pro_team_id=team,
        injury_status=status,
        lineup_locked=locked,
        nfl_active=True,
    )


class World:
    """Fake ESPN + schedule, patched in at the lineup service boundary."""

    def __init__(self, monkeypatch, *, now, rosters, projections):
        # `rosters` is a list consumed one per ESPN roster read (the
        # last one repeats), to simulate data changing between reads.
        self.now = now
        self.rosters = rosters
        self.reads = 0
        self.client = SimpleNamespace(
            settings=SimpleNamespace(espn_season=2026, espn_swid="{x}"),
            league_id=1,
        )
        self.submitted: list[list[LineupMove]] = []
        self.records: list[dict] = []

        world = self

        def load_roster(_client, team_id):
            idx = min(world.reads, len(world.rosters) - 1)
            world.reads += 1
            return TeamRoster(
                team_id=team_id,
                team_name="us",
                entries=[e.model_copy() for e in world.rosters[idx]],
                fetched_at=world.now,
            )

        def on_bye(_season, team, *, week):
            if team == ON_BYE_TEAM:
                return True
            if team == UNKNOWN_SCHEDULE_TEAM:
                return None
            return False

        def kickoff(_season, team, *, week, force=False):
            if team == LATE_GAME_TEAM:
                return KICKOFF + timedelta(hours=3)
            return KICKOFF

        monkeypatch.setattr(
            service, "load_lineup_settings", lambda c: (SLOTS, "INDIVIDUAL_GAME")
        )
        monkeypatch.setattr(service, "load_team_roster", load_roster)
        monkeypatch.setattr(
            service,
            "_load_weekly_projections",
            lambda c, entries, week: dict(projections),
        )
        monkeypatch.setattr(service, "is_team_on_bye", on_bye)
        monkeypatch.setattr(service, "team_kickoff_for_week", kickoff)
        monkeypatch.setattr(service, "week_first_kickoff", lambda s, week: KICKOFF)
        monkeypatch.setattr(service, "_mark_lineup_attempt", lambda: None)

        class RecordingTxn(ESPNTransactionsClient):
            def _post(self, body, *, confirm, label):
                world.submitted.append(
                    [
                        LineupMove(
                            i["playerId"],
                            i["fromLineupSlotId"],
                            i["toLineupSlotId"],
                        )
                        for i in body["items"]
                    ]
                )
                return {"ok": True}

        self.txn = RecordingTxn(
            self.client, settings=TransactionsSettings(mode="live")
        )

        class Store:
            def record(self, **kwargs):
                world.records.append(kwargs)

        self.store = Store()

    def submit(self, **kwargs):
        return service.submit_lineup_safely(
            self.client,
            team_id=1,
            week=7,
            confirm=True,
            now_fn=lambda: self.now,
            txn=self.txn,
            decision_store=self.store,
            context_deltas={},
            **kwargs,
        )

    def final_slots(self) -> dict[int, int]:
        slots = {e.player_id: e.lineup_slot_id for e in self.rosters[-1]}
        for moves in self.submitted:
            for m in moves:
                slots[m.player_id] = m.to_slot_id
        return slots


def base_roster(*overrides: RosterEntry):
    roster = {
        1: player(1, QB, QB),
        2: player(2, QB, BENCH),
        10: player(10, RB, RB),
        11: player(11, RB, BENCH),
        20: player(20, WR, WR),
        21: player(21, WR, FLEX),
        22: player(22, WR, BENCH),
    }
    roster.update({e.player_id: e for e in overrides})
    return list(roster.values())


BASE_PROJECTIONS = {1: 20, 2: 12, 10: 15, 11: 8, 20: 14, 21: 11, 22: 6}


def test_confirmed_out_starter_is_replaced(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "OUT"))],
        projections=BASE_PROJECTIONS,
    )
    result = world.submit()

    assert result.outcome == "submitted"
    slots = world.final_slots()
    assert slots[1] == BENCH
    assert slots[2] == QB


def test_ir_player_never_starts_despite_top_projection(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(22, WR, BENCH, "INJURY_RESERVE"))],
        projections={**BASE_PROJECTIONS, 22: 40},
    )
    world.submit()
    assert world.final_slots()[22] == BENCH


def test_bye_week_starter_is_benched(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(20, WR, WR, team=ON_BYE_TEAM))],
        projections=BASE_PROJECTIONS,
    )
    world.submit()
    slots = world.final_slots()
    assert slots[20] == BENCH
    assert slots[21] == WR
    assert slots[11] == FLEX  # RB 8 pts beats WR 6 pts for FLEX


def test_confirmed_active_questionable_starts_without_discount(monkeypatch):
    world = World(
        monkeypatch,
        now=POST_INACTIVES,
        rosters=[base_roster(player(1, QB, BENCH, "QUESTIONABLE"),
                                player(2, QB, QB))],
        projections=BASE_PROJECTIONS,
    )
    result = world.submit()
    assert world.final_slots()[1] == QB
    candidate = result.plan.candidates[1]
    assert candidate.availability.confirmed_active
    assert candidate.projected_points == 20


def test_provisional_questionable_start_is_flagged_for_revalidation(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "QUESTIONABLE"))],
        projections=BASE_PROJECTIONS,
    )
    result = world.submit()
    assert world.final_slots()[1] == QB  # 17 discounted still beats 12
    assert 1 in result.validation.revalidate_player_ids


def test_unknown_availability_never_starts(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, None),
                                player(10, RB, RB, team=UNKNOWN_SCHEDULE_TEAM))],
        projections=BASE_PROJECTIONS,
    )
    result = world.submit()
    slots = world.final_slots()
    assert slots[1] == BENCH
    assert slots[2] == QB
    assert result.plan.candidates[10].locked  # unknown schedule => locked
    assert slots[10] == RB  # can't be moved, stays put


def test_locked_players_are_never_moved(monkeypatch):
    # P1 (QB) is locked and OUT in the starting slot; P22 (WR) is locked
    # on the bench despite the best projection.
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "OUT", locked=True),
                                player(22, WR, BENCH, locked=True))],
        projections={**BASE_PROJECTIONS, 22: 40},
    )
    result = world.submit()
    moved = {m.player_id for moves in world.submitted for m in moves}
    assert 1 not in moved and 22 not in moved
    assert any("locked" in w for w in result.validation.warnings)


def test_individual_game_lock_from_schedule(monkeypatch):
    world = World(
        monkeypatch,
        now=KICKOFF + timedelta(minutes=5),
        rosters=[base_roster(player(20, WR, WR, team=LATE_GAME_TEAM),
                                player(22, WR, BENCH, team=LATE_GAME_TEAM))],
        projections={**BASE_PROJECTIONS, 22: 30},
    )
    result = world.submit()
    assert result.plan.candidates[1].locked
    assert not result.plan.candidates[22].locked
    assert {m.player_id for moves in world.submitted for m in moves} <= {20, 21, 22}


def test_no_startable_qb_leaves_slot_empty_and_logs_critical(monkeypatch, caplog):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "OUT"),
                                player(2, QB, BENCH, "INJURY_RESERVE"))],
        projections=BASE_PROJECTIONS,
    )
    with caplog.at_level(logging.CRITICAL):
        result = world.submit()

    slots = world.final_slots()
    assert slots[1] == BENCH and slots[2] == BENCH
    assert result.plan.unfilled_slots == {QB: 1}
    assert any("lineup.slot_unfilled" in r.message for r in caplog.records)


def test_status_change_before_submission_triggers_repair(monkeypatch):
    # Plan read: P1 healthy. Final fresh read: P1 now OUT -> the plan is
    # rejected and rebuilt from fresh data on attempt 2.
    healthy = base_roster()
    out = base_roster(player(1, QB, QB, "OUT"))
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[healthy, out, out],
        projections=BASE_PROJECTIONS,
    )
    result = world.submit()
    assert result.attempts == 2
    assert result.outcome == "submitted"
    assert world.final_slots()[1] == BENCH


def test_invalid_lineup_is_never_submitted(monkeypatch, caplog):
    # Every final read disagrees with the plan read -> no attempt can be
    # validated, so nothing reaches ESPN.
    healthy = base_roster()
    out = base_roster(player(1, QB, QB, "OUT"))
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[healthy, out, healthy, out, healthy, out],
        projections=BASE_PROJECTIONS,
    )
    with caplog.at_level(logging.CRITICAL):
        result = world.submit(max_attempts=3)
    assert result.outcome == "no_legal_lineup"
    assert world.submitted == []
    assert world.records[-1]["outcome"] == "no_legal_lineup"
    assert any("lineup.no_legal_lineup" in r.message for r in caplog.records)


def test_transactions_client_refuses_unvalidated_lineups():
    client = SimpleNamespace(
        settings=SimpleNamespace(espn_season=2026, espn_swid="{x}"),
        league_id=1,
    )
    txn = ESPNTransactionsClient(
        client, settings=TransactionsSettings(mode="shadow")
    )
    moves = [LineupMove(1, 20, 0)]

    with pytest.raises(UnvalidatedLineupError):
        txn.set_lineup(team_id=1, moves=moves, scoring_period_id=7, validation=None)

    with pytest.raises(UnvalidatedLineupError):
        txn.set_lineup(
            team_id=1,
            moves=moves,
            scoring_period_id=7,
            validation=LineupValidation(errors=["bad"], moves=tuple(moves)),
        )

    with pytest.raises(UnvalidatedLineupError):
        txn.set_lineup(
            team_id=1,
            moves=moves,
            scoring_period_id=7,
            validation=LineupValidation(moves=(LineupMove(2, 20, 0),)),
        )


def test_validator_rejects_manual_start_of_out_player(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(2, QB, BENCH, "OUT"))],
        projections=BASE_PROJECTIONS,
    )
    candidates, slots = service.load_lineup_candidates(
        world.client, team_id=1, week=7, now_fn=lambda: world.now,
        with_projections=False,
    )
    validation = validate_assignments({2: QB, 1: BENCH}, candidates, slots)
    assert not validation.ok
    assert any("P2 can't start" in e for e in validation.errors)


def test_lineup_gate_works_without_openai_or_database(monkeypatch):
    import openai

    def boom(*a, **k):
        raise RuntimeError("OpenAI must not be called by the lineup gate")

    monkeypatch.setattr(openai, "OpenAI", boom)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "OUT"))],
        projections=BASE_PROJECTIONS,
    )
    result = service.submit_lineup_safely(
        world.client,
        team_id=1,
        week=7,
        confirm=True,
        now_fn=lambda: world.now,
        txn=world.txn,
        decision_store=world.store,
    )  # context_deltas omitted -> loader hits the missing DB and degrades
    assert result.outcome == "submitted"
    assert world.final_slots()[2] == QB


def test_health_check_flags_unlocked_ineligible_starter_only(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster(player(1, QB, QB, "OUT"),
                                player(20, WR, WR, "OUT", locked=True))],
        projections=BASE_PROJECTIONS,
    )
    candidates, slots = service.load_lineup_candidates(
        world.client, team_id=1, week=7, now_fn=lambda: world.now,
        with_projections=False,
    )
    assert service.find_lineup_problems(candidates, slots) == ["starter:1"]


def test_health_check_quiet_when_lineup_is_fine(monkeypatch):
    world = World(
        monkeypatch,
        now=PRE_INACTIVES,
        rosters=[base_roster()],
        projections=BASE_PROJECTIONS,
    )
    candidates, slots = service.load_lineup_candidates(
        world.client, team_id=1, week=7, now_fn=lambda: world.now,
        with_projections=False,
    )
    assert service.find_lineup_problems(candidates, slots) == []
