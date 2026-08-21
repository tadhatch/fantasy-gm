from __future__ import annotations

from dataclasses import dataclass

import nflreadpy as nfl


@dataclass(slots=True)
class DepthInfo:
    espn_id: int
    team: str | None = None
    position: str | None = None
    depth_rank: int | None = None
    depth_slot: int | None = None


def build_depth_index(season: int = 2026) -> dict[int, DepthInfo]:
    try:
        df = nfl.load_depth_charts(season)
    except Exception:
        return {}

    if "dt" in df.columns:
        df = df.sort("dt")

    result: dict[int, DepthInfo] = {}
    for row in df.to_dicts():
        espn_id = _int(row.get("espn_id"))
        if espn_id is None:
            continue

        result[espn_id] = DepthInfo(
            espn_id=espn_id,
            team=_str(row.get("team")),
            position=_str(row.get("pos_abb") or row.get("pos_name")),
            depth_rank=_int(row.get("pos_rank")),
            depth_slot=_int(row.get("pos_slot")),
        )

    return result


def opportunity_adjustment(info: DepthInfo | None) -> tuple[float, str | None]:
    if info is None or info.depth_rank is None:
        return 0.0, None

    if info.depth_rank == 1:
        return 2.5, "DEPTH1"
    if info.depth_rank == 2:
        return 0.5, "DEPTH2"
    if info.depth_rank == 3:
        return -0.5, "DEPTH3"
    return -2.0, f"DEPTH{info.depth_rank}"


def _int(value: object) -> int | None:
    try:
        return None if value is None else int(float(value))
    except (TypeError, ValueError):
        return None


def _str(value: object) -> str | None:
    return None if value is None else str(value)
