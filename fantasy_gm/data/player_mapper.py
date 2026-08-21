from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from .nflverse import load_draft_rankings, load_player_ids


@dataclass(slots=True)
class MarketData:
    espn_id: int
    fantasypros_id: str | None = None
    ecr: float | None = None
    ecr_sd: float | None = None
    ecr_best: float | None = None
    ecr_worst: float | None = None
    rank_delta: float | None = None
    bye: int | None = None


def _norm_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    return text


def build_market_index() -> dict[int, MarketData]:
    """
    Join ESPN -> FantasyPros using nflverse's ID table, then FantasyPros -> ECR.

    This avoids fuzzy name matching for the vast majority of players.
    """
    ids = load_player_ids()
    rankings = load_draft_rankings()

    id_rows = ids.select(
        [
            pl.col("espn_id").cast(pl.Utf8, strict=False),
            pl.col("fantasypros_id").cast(pl.Utf8, strict=False),
        ]
    ).to_dicts()

    fp_to_espn: dict[str, int] = {}
    for row in id_rows:
        espn = _norm_id(row.get("espn_id"))
        fp = _norm_id(row.get("fantasypros_id"))
        if espn and fp:
            try:
                fp_to_espn[fp] = int(espn)
            except ValueError:
                continue

    result: dict[int, MarketData] = {}

    for row in rankings.to_dicts():
        fp_id = _norm_id(row.get("id"))
        if not fp_id:
            continue

        espn_id = fp_to_espn.get(fp_id)
        if espn_id is None:
            continue

        # load_ff_rankings contains several page/ranking types. Prefer rows that
        # actually expose an ECR and keep the best (lowest) ECR we see.
        ecr = _float(row.get("ecr"))
        if ecr is None:
            continue

        existing = result.get(espn_id)
        if existing is not None and existing.ecr is not None and existing.ecr <= ecr:
            continue

        result[espn_id] = MarketData(
            espn_id=espn_id,
            fantasypros_id=fp_id,
            ecr=ecr,
            ecr_sd=_float(row.get("sd")),
            ecr_best=_float(row.get("best")),
            ecr_worst=_float(row.get("worst")),
            rank_delta=_float(row.get("rank_delta")),
            bye=_int(row.get("bye")),
        )

    return result


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None
