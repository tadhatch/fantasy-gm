from __future__ import annotations

import base64
from dataclasses import dataclass


RECORD_SIZE = 45


@dataclass(slots=True)
class InitPick:
    league_id: int
    team_id: int
    overall_pick: int
    player_id: int
    selection_type: int


def decode_init_picks(
    payload: str,
    *,
    league_id: int,
    max_picks: int = 400,
    min_run: int = 10,
) -> list[InitPick]:
    """
    Recover completed picks from ESPN's INIT snapshot.

    Observed draft-slot record layout:

        +12  league_id       uint32 BE
        +16  team_id         uint32 BE
        +20  overall_pick    uint32 BE
        +24  player_id       int32 BE
        +28  selection_type  uint32 BE

    Records are exactly 45 bytes apart.

    ESPN preallocates future slots using player_id == -1.
    """

    text = payload.strip()

    padding = "=" * (
        (4 - len(text) % 4) % 4
    )

    raw = base64.b64decode(
        text + padding
    )

    def parse_record(
        start: int,
    ) -> InitPick | None:
        if start < 0:
            return None

        if start + RECORD_SIZE > len(raw):
            return None

        record_league = int.from_bytes(
            raw[start + 12:start + 16],
            "big",
            signed=False,
        )

        if record_league != league_id:
            return None

        team_id = int.from_bytes(
            raw[start + 16:start + 20],
            "big",
            signed=False,
        )

        overall_pick = int.from_bytes(
            raw[start + 20:start + 24],
            "big",
            signed=False,
        )

        player_id = int.from_bytes(
            raw[start + 24:start + 28],
            "big",
            signed=True,
        )

        selection_type = int.from_bytes(
            raw[start + 28:start + 32],
            "big",
            signed=False,
        )

        if not (
            1 <= team_id <= 32
            and
            1 <= overall_pick <= max_picks
        ):
            return None

        return InitPick(
            league_id=record_league,
            team_id=team_id,
            overall_pick=overall_pick,
            player_id=player_id,
            selection_type=selection_type,
        )

    #
    # Locate the REAL draft table.
    #
    # We specifically look for a record whose overall pick is 1,
    # then require subsequent 45-byte records to contain picks
    # 2, 3, 4, ... in exact sequence.
    #
    best_run: list[InitPick] = []

    for start in range(
        0,
        len(raw) - RECORD_SIZE + 1,
    ):
        first = parse_record(start)

        if (
            first is None
            or first.overall_pick != 1
        ):
            continue

        run: list[InitPick] = [
            first
        ]

        expected_pick = 2
        pos = start + RECORD_SIZE

        while (
            pos + RECORD_SIZE
            <= len(raw)
        ):
            record = parse_record(pos)

            if record is None:
                break

            if (
                record.overall_pick
                != expected_pick
            ):
                break

            run.append(record)

            expected_pick += 1
            pos += RECORD_SIZE

        if len(run) > len(best_run):
            best_run = run

    if len(best_run) < min_run:
        raise ValueError(
            "Could not locate ESPN INIT draft-slot table. "
            f"Longest sequential run was {len(best_run)} records."
        )

    #
    # The table contains both completed and future slots.
    #
    # -1 == unfilled future slot
    #  0 == no player
    #
    # Other negatives are valid ESPN D/ST IDs.
    #
    completed = [
        record
        for record in best_run
        if record.player_id not in (
            -1,
            0,
        )
    ]

    return completed