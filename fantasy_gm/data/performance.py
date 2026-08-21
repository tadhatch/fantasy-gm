from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import nflreadpy as nfl


@dataclass(slots=True)
class PerformanceMetrics:
    espn_id: int
    snap_share: float | None = None
    target_share: float | None = None
    rush_share: float | None = None
    red_zone_share: float | None = None
    yards_per_route_run: float | None = None
    rush_yards_over_expected: float | None = None
    receiving_epa_per_target: float | None = None


@lru_cache(maxsize=1)
def _id_maps() -> tuple[dict[str, int], dict[str, int]]:
    ids = nfl.load_ff_playerids()
    gsis_to_espn: dict[str, int] = {}
    pfr_to_espn: dict[str, int] = {}

    for row in ids.to_dicts():
        espn = _norm(row.get("espn_id"))
        if not espn:
            continue
        try:
            espn_id = int(espn)
        except ValueError:
            continue

        gsis = _norm(row.get("gsis_id"))
        pfr = _norm(row.get("pfr_id"))
        if gsis:
            gsis_to_espn[gsis] = espn_id
        if pfr:
            pfr_to_espn[pfr] = espn_id

    return gsis_to_espn, pfr_to_espn


def build_performance_index(season: int = 2025) -> dict[int, PerformanceMetrics]:
    result: dict[int, PerformanceMetrics] = {}
    _merge_snap_counts(result, season)
    _merge_player_usage(result, season)
    _merge_nextgen(result, season)
    return result


def _metric(result: dict[int, PerformanceMetrics], espn_id: int) -> PerformanceMetrics:
    if espn_id not in result:
        result[espn_id] = PerformanceMetrics(espn_id=espn_id)
    return result[espn_id]


def _merge_snap_counts(result: dict[int, PerformanceMetrics], season: int) -> None:
    try:
        df = nfl.load_snap_counts(season)
    except Exception:
        return

    _, pfr_to_espn = _id_maps()
    buckets: dict[int, list[tuple[float, float]]] = {}

    for row in df.to_dicts():
        pfr = _norm(row.get("pfr_player_id"))
        espn_id = pfr_to_espn.get(pfr or "")
        pct = _float(row.get("offense_pct"))
        if espn_id is None or pct is None:
            continue

        weight = max(1.0, _float(row.get("offense_snaps")) or 1.0)
        buckets.setdefault(espn_id, []).append((pct, weight))

    for espn_id, vals in buckets.items():
        denom = sum(weight for _, weight in vals)
        if not denom:
            continue
        avg = sum(value * weight for value, weight in vals) / denom
        if avg > 1.5:
            avg /= 100.0
        _metric(result, espn_id).snap_share = _clamp01(avg)


def _merge_player_usage(result: dict[int, PerformanceMetrics], season: int) -> None:
    try:
        df = nfl.load_player_stats(season, summary_level="reg")
    except Exception:
        return

    gsis_to_espn, _ = _id_maps()
    rows = df.to_dicts()

    team_targets: dict[str, float] = {}
    team_carries: dict[str, float] = {}
    team_rz: dict[str, float] = {}

    for row in rows:
        team = str(row.get("team") or "")
        team_targets[team] = team_targets.get(team, 0.0) + (_float(row.get("targets")) or 0.0)
        team_carries[team] = team_carries.get(team, 0.0) + (_float(row.get("carries")) or 0.0)

        rz = (
            (_float(row.get("carries_inside_10")) or 0.0)
            + (_float(row.get("targets_inside_10")) or 0.0)
        )
        team_rz[team] = team_rz.get(team, 0.0) + rz

    for row in rows:
        gsis = _norm(row.get("player_id"))
        espn_id = gsis_to_espn.get(gsis or "")
        if espn_id is None:
            continue

        team = str(row.get("team") or "")
        metric = _metric(result, espn_id)

        targets = _float(row.get("targets")) or 0.0
        carries = _float(row.get("carries")) or 0.0

        if team_targets.get(team, 0.0) > 0:
            metric.target_share = _clamp01(targets / team_targets[team])

        if team_carries.get(team, 0.0) > 0:
            metric.rush_share = _clamp01(carries / team_carries[team])

        rz = (
            (_float(row.get("carries_inside_10")) or 0.0)
            + (_float(row.get("targets_inside_10")) or 0.0)
        )
        if team_rz.get(team, 0.0) > 0:
            metric.red_zone_share = _clamp01(rz / team_rz[team])

        routes = _float(row.get("routes"))
        receiving_yards = _float(row.get("receiving_yards"))
        if routes and routes > 0 and receiving_yards is not None:
            metric.yards_per_route_run = receiving_yards / routes

        receiving_epa = _float(row.get("receiving_epa"))
        if targets > 0 and receiving_epa is not None:
            metric.receiving_epa_per_target = receiving_epa / targets


def _merge_nextgen(result: dict[int, PerformanceMetrics], season: int) -> None:
    gsis_to_espn, _ = _id_maps()

    try:
        rush = nfl.load_nextgen_stats(season, stat_type="rushing")
    except Exception:
        return

    buckets: dict[int, list[tuple[float, float]]] = {}

    for row in rush.to_dicts():
        gsis = _norm(row.get("player_gsis_id") or row.get("player_id"))
        espn_id = gsis_to_espn.get(gsis or "")
        ryoe = _float(row.get("rush_yards_over_expected_per_att"))
        attempts = _float(row.get("rush_attempts")) or 1.0

        if espn_id is None or ryoe is None:
            continue
        buckets.setdefault(espn_id, []).append((ryoe, attempts))

    for espn_id, vals in buckets.items():
        denom = sum(weight for _, weight in vals)
        if denom:
            _metric(result, espn_id).rush_yards_over_expected = (
                sum(value * weight for value, weight in vals) / denom
            )


def _norm(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null"}:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
