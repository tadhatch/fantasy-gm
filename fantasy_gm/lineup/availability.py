# fantasy_gm/lineup/availability.py
#
# The single, deterministic answer to "may this player be started this
# scoring period?" -- a hard eligibility requirement, separate from (and
# always applied before) any ranking/grading of who *should* start. No
# LLM, no database, no network: everything it needs is passed in, so it
# keeps working when OpenAI is rate-limited or the API budget is spent.
#
# Source of truth is ESPN (confirmed against a live mRoster payload):
#   - player.injuryStatus: ACTIVE, QUESTIONABLE, DAY_TO_DAY, DOUBTFUL,
#     OUT, INJURY_RESERVE, SUSPENSION, ... -- absent entirely for D/ST.
#   - player.active: whether ESPN has him on an active NFL roster.
#   - lineupSlotId 21: our own IR slot.
# Bye weeks and kickoff times come from the nflverse schedule.
#
# "Confirmed active" is an inference, not a field ESPN exposes: NFL
# inactive lists are published ~90 minutes before kickoff and ESPN flips
# a ruled-out player to OUT when they are. So an ESPN status *read after
# that point* that still isn't OUT is treated as confirmed active. A
# status read before that point -- or a status that's missing -- never
# is.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from fantasy_gm.config import env_float

IR_SLOT_ID = 21
DST_POSITION_ID = 16

# Statuses that make a player unable to start, period. Includes spellings
# ESPN uses today plus ones it has used/may use for the same designation
# -- an unrecognised status falls through to UNKNOWN (not startable),
# never to "healthy".
INELIGIBLE_STATUSES = {
    "OUT",
    "INJURY_RESERVE",
    "IR",
    "SUSPENSION",
    "SUSPENDED",
    "PUP",
    "PHYSICALLY_UNABLE_TO_PERFORM",
    "NFI",
    "NON_FOOTBALL_INJURY",
    "NON_FOOTBALL_ILLNESS",
    "INACTIVE",
    "RESERVE",
}
HEALTHY_STATUSES = {"ACTIVE", "NORMAL"}
# Game-time decisions: may start provisionally before inactives, must be
# revalidated after.
PROVISIONAL_STATUSES = {"QUESTIONABLE", "DAY_TO_DAY", "PROBABLE"}
# May start only once confirmed active after inactives.
CONFIRMATION_REQUIRED_STATUSES = {"DOUBTFUL"}


class AvailabilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    # May start now, but must be revalidated once inactives are out.
    PROVISIONAL = "provisional"
    INELIGIBLE = "ineligible"
    # Not enough trustworthy data to say -- never startable.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Availability:
    status: AvailabilityStatus
    reason: str
    injury_status: str | None
    source: str
    observed_at: datetime | None
    requires_revalidation: bool = False
    # True only when inferred from a post-inactives ESPN read.
    confirmed_active: bool = False

    @property
    def can_start(self) -> bool:
        return self.status in (
            AvailabilityStatus.ELIGIBLE,
            AvailabilityStatus.PROVISIONAL,
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "injury_status": self.injury_status,
            "source": self.source,
            "observed_at": (
                self.observed_at.isoformat() if self.observed_at else None
            ),
            "requires_revalidation": self.requires_revalidation,
            "confirmed_active": self.confirmed_active,
        }


@dataclass(frozen=True, slots=True)
class AvailabilitySettings:
    # NFL inactives are published 90 minutes before kickoff.
    inactives_lead: timedelta = timedelta(minutes=90)
    # ESPN data older than this can't support any availability call.
    max_data_age: timedelta = timedelta(minutes=20)

    @classmethod
    def from_env(cls) -> "AvailabilitySettings":
        return cls(
            inactives_lead=timedelta(
                minutes=env_float("FANTASY_GM_INACTIVES_LEAD_MINUTES", 90)
            ),
            max_data_age=timedelta(
                minutes=env_float(
                    "FANTASY_GM_AVAILABILITY_MAX_AGE_MINUTES", 20
                )
            ),
        )


def evaluate_availability(
    *,
    injury_status: str | None,
    position_id: int | None,
    lineup_slot_id: int | None,
    nfl_active: bool | None,
    on_bye: bool | None,
    kickoff: datetime | None,
    observed_at: datetime | None,
    now: datetime,
    settings: AvailabilitySettings | None = None,
    source: str = "espn",
) -> Availability:
    settings = settings or AvailabilitySettings()
    status = (injury_status or "").strip().upper() or None

    def result(
        kind: AvailabilityStatus,
        reason: str,
        *,
        revalidate: bool = False,
        confirmed: bool = False,
    ) -> Availability:
        return Availability(
            status=kind,
            reason=reason,
            injury_status=status,
            source=source,
            observed_at=observed_at,
            requires_revalidation=revalidate,
            confirmed_active=confirmed,
        )

    if lineup_slot_id == IR_SLOT_ID:
        return result(AvailabilityStatus.INELIGIBLE, "in our IR slot")

    if status in INELIGIBLE_STATUSES:
        return result(AvailabilityStatus.INELIGIBLE, f"ESPN status {status}")

    if nfl_active is False:
        return result(
            AvailabilityStatus.INELIGIBLE,
            "not on an active NFL roster per ESPN",
        )

    if on_bye is True:
        return result(AvailabilityStatus.INELIGIBLE, "on bye this week")
    if on_bye is None:
        return result(
            AvailabilityStatus.UNKNOWN,
            "NFL schedule unavailable -- can't confirm he has a game",
        )

    if observed_at is None:
        return result(
            AvailabilityStatus.UNKNOWN, "no timestamp on ESPN data"
        )
    if now - observed_at > settings.max_data_age:
        return result(
            AvailabilityStatus.UNKNOWN,
            f"ESPN data is stale (read {observed_at.isoformat()})",
        )

    if status is None:
        # Verified live: ESPN never sends an injuryStatus for a team
        # defense -- a unit can't be injured. For anyone else a missing
        # status is missing data, not a clean bill of health.
        if position_id == DST_POSITION_ID:
            status = "ACTIVE"
        else:
            return result(
                AvailabilityStatus.UNKNOWN, "ESPN reported no injury status"
            )

    inactives_at = kickoff - settings.inactives_lead if kickoff else None
    post_inactives_read = (
        inactives_at is not None
        and now >= inactives_at
        and observed_at >= inactives_at
    )

    if status in HEALTHY_STATUSES:
        if post_inactives_read:
            return result(
                AvailabilityStatus.ELIGIBLE,
                "confirmed active (not ruled out after inactives)",
                confirmed=True,
            )
        return result(AvailabilityStatus.ELIGIBLE, "no injury designation")

    if status in PROVISIONAL_STATUSES:
        if post_inactives_read:
            return result(
                AvailabilityStatus.ELIGIBLE,
                f"{status} but confirmed active (not ruled out after "
                "inactives)",
                confirmed=True,
            )
        return result(
            AvailabilityStatus.PROVISIONAL,
            f"{status} -- provisional until inactives are published",
            revalidate=True,
        )

    if status in CONFIRMATION_REQUIRED_STATUSES:
        if post_inactives_read:
            return result(
                AvailabilityStatus.ELIGIBLE,
                f"{status} but confirmed active (not ruled out after "
                "inactives)",
                confirmed=True,
            )
        return result(
            AvailabilityStatus.INELIGIBLE,
            f"{status} -- may start only once confirmed active after "
            "inactives",
            revalidate=True,
        )

    return result(
        AvailabilityStatus.UNKNOWN, f"unrecognised ESPN status {status}"
    )
