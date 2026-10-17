from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fantasy_gm.lineup.availability import (
    AvailabilityStatus,
    evaluate_availability,
)

KICKOFF = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)  # 1pm ET
PRE_INACTIVES = KICKOFF - timedelta(hours=2)  # before the 90-min mark
POST_INACTIVES = KICKOFF - timedelta(minutes=60)


def _eval(
    status,
    *,
    now=PRE_INACTIVES,
    observed_at=None,
    position_id=1,
    slot=20,
    nfl_active=True,
    on_bye=False,
    kickoff=KICKOFF,
):
    return evaluate_availability(
        injury_status=status,
        position_id=position_id,
        lineup_slot_id=slot,
        nfl_active=nfl_active,
        on_bye=on_bye,
        kickoff=kickoff,
        observed_at=observed_at or now,
        now=now,
    )


@pytest.mark.parametrize(
    "status",
    ["INJURY_RESERVE", "OUT", "SUSPENSION", "SUSPENDED", "PUP", "NFI"],
)
def test_hard_out_statuses_cannot_start(status):
    result = _eval(status)
    assert result.status == AvailabilityStatus.INELIGIBLE
    assert not result.can_start


def test_ir_cannot_start_even_after_inactives():
    assert not _eval("INJURY_RESERVE", now=POST_INACTIVES).can_start


def test_player_in_our_ir_slot_cannot_start():
    assert not _eval("ACTIVE", slot=21).can_start


def test_bye_week_player_cannot_start():
    result = _eval("ACTIVE", on_bye=True, kickoff=None)
    assert result.status == AvailabilityStatus.INELIGIBLE
    assert "bye" in result.reason


def test_unknown_schedule_is_not_treated_as_playing():
    result = _eval("ACTIVE", on_bye=None, kickoff=None)
    assert result.status == AvailabilityStatus.UNKNOWN
    assert not result.can_start


def test_not_on_active_nfl_roster_cannot_start():
    assert not _eval("ACTIVE", nfl_active=False).can_start


def test_healthy_player_can_start():
    result = _eval("ACTIVE")
    assert result.status == AvailabilityStatus.ELIGIBLE
    assert not result.requires_revalidation


def test_questionable_before_inactives_is_provisional_and_flagged():
    result = _eval("QUESTIONABLE")
    assert result.status == AvailabilityStatus.PROVISIONAL
    assert result.can_start
    assert result.requires_revalidation
    assert not result.confirmed_active


def test_questionable_confirmed_active_after_inactives_can_start():
    # Still designated QUESTIONABLE on ESPN, but read after inactives and
    # not ruled out -> confirmed active, not rejected for the label.
    result = _eval("QUESTIONABLE", now=POST_INACTIVES)
    assert result.status == AvailabilityStatus.ELIGIBLE
    assert result.confirmed_active
    assert not result.requires_revalidation


def test_questionable_read_before_inactives_is_not_confirmation():
    # It's after inactives *now*, but the data was read before them.
    result = _eval(
        "QUESTIONABLE",
        now=KICKOFF - timedelta(minutes=85),
        observed_at=KICKOFF - timedelta(minutes=95),
    )
    assert result.status == AvailabilityStatus.PROVISIONAL
    assert not result.confirmed_active


def test_doubtful_cannot_start_until_confirmed_active():
    before = _eval("DOUBTFUL")
    assert not before.can_start
    assert before.requires_revalidation

    after = _eval("DOUBTFUL", now=POST_INACTIVES)
    assert after.can_start
    assert after.confirmed_active


def test_missing_status_is_unknown_not_active():
    result = _eval(None)
    assert result.status == AvailabilityStatus.UNKNOWN
    assert not result.can_start


def test_missing_status_for_dst_is_healthy():
    # Verified against live ESPN data: D/ST never has an injuryStatus.
    assert _eval(None, position_id=16).can_start


def test_stale_espn_data_is_unknown():
    result = _eval(
        "ACTIVE",
        now=PRE_INACTIVES,
        observed_at=PRE_INACTIVES - timedelta(hours=3),
    )
    assert result.status == AvailabilityStatus.UNKNOWN
    assert "stale" in result.reason


def test_unrecognised_status_is_unknown():
    assert _eval("SOMETHING_NEW").status == AvailabilityStatus.UNKNOWN


def test_out_is_ineligible_even_with_stale_data():
    # A known-bad status stays ineligible; staleness only ever blocks
    # *confirming* availability.
    result = _eval(
        "OUT", observed_at=PRE_INACTIVES - timedelta(hours=3)
    )
    assert result.status == AvailabilityStatus.INELIGIBLE
