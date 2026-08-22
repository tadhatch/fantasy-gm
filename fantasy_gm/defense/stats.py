from __future__ import annotations

import nflreadpy as nfl
import polars as pl

from fantasy_gm.espn.constants import PRO_TEAM_ABBR
from .models import DefenseStats


NFLVERSE_TO_ESPN = {
    "JAX": "JAX",
    "LA": "LAR",
    "LAR": "LAR",
    "LV": "LV",
    "OAK": "LV",
    "SD": "LAC",
    "LAC": "LAC",
    "STL": "LAR",
    "WAS": "WSH",
    "WSH": "WSH",
}

ESPN_TEAM_ID_BY_ABBR = {
    abbr: team_id
    for team_id, abbr in PRO_TEAM_ABBR.items()
}


def _abbr(team: str | None) -> str | None:
    if not team:
        return None

    team = str(team).upper()

    return NFLVERSE_TO_ESPN.get(
        team,
        team,
    )


def _normalize_team_column(
    df: pl.DataFrame,
    column: str,
) -> pl.DataFrame:
    return df.with_columns(
        pl.col(column)
        .cast(pl.Utf8)
        .map_elements(
            _abbr,
            return_dtype=pl.Utf8,
        )
        .alias(column)
    )


def _ensure_numeric_columns(
    df: pl.DataFrame,
    columns: list[str],
) -> pl.DataFrame:
    expressions: list[pl.Expr] = []

    for column in columns:
        if column in df.columns:
            expressions.append(
                pl.col(column)
                .cast(
                    pl.Float64,
                    strict=False,
                )
                .fill_null(0.0)
                .alias(column)
            )
        else:
            expressions.append(
                pl.lit(0.0).alias(column)
            )

    return df.with_columns(expressions)


def _z_score_map(
    df: pl.DataFrame,
    *,
    key: str,
    value: str,
) -> dict[str, float]:
    if df.is_empty():
        return {}

    stats = df.select(
        pl.col(value).mean().alias("mean"),
        pl.col(value).std(ddof=0).alias("std"),
    ).row(
        0,
        named=True,
    )

    mean = float(
        stats["mean"] or 0.0
    )

    std = float(
        stats["std"] or 0.0
    )

    if std <= 1e-9:
        return {
            row[key]: 0.0
            for row in df.select(
                key,
            ).iter_rows(
                named=True,
            )
        }

    z_df = df.select(
        pl.col(key),
        (
            (
                pl.col(value)
                - mean
            )
            / std
        ).alias("z"),
    )

    return {
        row[key]: float(row["z"])
        for row in z_df.iter_rows(
            named=True,
        )
    }


def load_defense_stats(
    *,
    base_season: int,
    schedule_season: int,
    early_weeks: int = 4,
) -> list[DefenseStats]:
    """
    Build one DefenseStats row per NFL team.

    Base performance:
      prior regular-season play-by-play

    Schedule:
      target-season schedule scored using prior-season
      offensive vulnerability.

    Example for the 2026 draft:

      base_season=2025
      schedule_season=2026
    """

    pbp = nfl.load_pbp(
        base_season,
    )

    schedules = nfl.load_schedules(
        schedule_season,
    )

    base_schedule = nfl.load_schedules(
        base_season,
    )

    # ---------------------------------------------------------
    # Regular-season filtering
    # ---------------------------------------------------------

    if "season_type" in pbp.columns:
        pbp = pbp.filter(
            pl.col("season_type") == "REG"
        )

    if "game_type" in schedules.columns:
        schedules = schedules.filter(
            pl.col("game_type") == "REG"
        )

    if "game_type" in base_schedule.columns:
        base_schedule = base_schedule.filter(
            pl.col("game_type") == "REG"
        )

    # ---------------------------------------------------------
    # Normalize team abbreviations
    # ---------------------------------------------------------

    for column in (
        "posteam",
        "defteam",
    ):
        if column in pbp.columns:
            pbp = _normalize_team_column(
                pbp,
                column,
            )

    for column in (
        "home_team",
        "away_team",
    ):
        if column in schedules.columns:
            schedules = _normalize_team_column(
                schedules,
                column,
            )

        if column in base_schedule.columns:
            base_schedule = _normalize_team_column(
                base_schedule,
                column,
            )

    # ---------------------------------------------------------
    # Defensive PBP
    # ---------------------------------------------------------

    defense_pbp = pbp.filter(
        pl.col("defteam").is_not_null()
    )

    defense_pbp = _ensure_numeric_columns(
        defense_pbp,
        [
            "sack",
            "interception",
            "fumble_lost",
            "touchdown",
            "return_touchdown",
            "pass_attempt",
            "yards_gained",
        ],
    )

    # Prefer nflverse's explicit return_touchdown flag.
    # If the field is entirely empty/zero, fall back to a
    # turnover + touchdown proxy.
    return_td_total = (
        defense_pbp.select(
            pl.col(
                "return_touchdown"
            ).sum()
        ).item()
        or 0.0
    )

    if return_td_total > 0:
        defense_pbp = (
            defense_pbp.with_columns(
                pl.col(
                    "return_touchdown"
                ).alias("def_td")
            )
        )
    else:
        defense_pbp = (
            defense_pbp.with_columns(
                (
                    (
                        (
                            pl.col(
                                "interception"
                            )
                            > 0
                        )
                        |
                        (
                            pl.col(
                                "fumble_lost"
                            )
                            > 0
                        )
                    )
                    &
                    (
                        pl.col(
                            "touchdown"
                        )
                        > 0
                    )
                )
                .cast(
                    pl.Float64
                )
                .alias("def_td")
            )
        )

    # ---------------------------------------------------------
    # Aggregate defense by game
    # ---------------------------------------------------------

    game_def = (
        defense_pbp
        .group_by(
            [
                "game_id",
                "defteam",
            ]
        )
        .agg(
            pl.col(
                "sack"
            ).sum().alias(
                "sacks"
            ),

            pl.col(
                "interception"
            ).sum().alias(
                "interceptions"
            ),

            pl.col(
                "fumble_lost"
            ).sum().alias(
                "fumbles_lost"
            ),

            pl.col(
                "def_td"
            ).sum().alias(
                "defensive_tds"
            ),

            pl.col(
                "yards_gained"
            ).sum().alias(
                "opponent_yards"
            ),

            pl.col(
                "pass_attempt"
            ).sum().alias(
                "pass_attempts"
            ),
        )
        .with_columns(
            (
                pl.col(
                    "interceptions"
                )
                +
                pl.col(
                    "fumbles_lost"
                )
            ).alias(
                "takeaways"
            )
        )
        .with_columns(
            (
                pl.col("sacks")
                /
                (
                    pl.col(
                        "pass_attempts"
                    )
                    +
                    pl.col(
                        "sacks"
                    )
                )
                .clip(
                    lower_bound=1.0
                )
            ).alias(
                "pressure_proxy"
            )
        )
    )

    # ---------------------------------------------------------
    # Points allowed from completed game scores
    # ---------------------------------------------------------

    score_rows: list[dict] = []

    for game in base_schedule.iter_rows(
        named=True,
    ):
        home_score = game.get(
            "home_score"
        )

        away_score = game.get(
            "away_score"
        )

        if (
            home_score is None
            or away_score is None
        ):
            continue

        score_rows.append(
            {
                "game_id": game[
                    "game_id"
                ],
                "defteam": game[
                    "home_team"
                ],
                "points_allowed": float(
                    away_score
                ),
            }
        )

        score_rows.append(
            {
                "game_id": game[
                    "game_id"
                ],
                "defteam": game[
                    "away_team"
                ],
                "points_allowed": float(
                    home_score
                ),
            }
        )

    if score_rows:
        score_df = pl.DataFrame(
            score_rows
        )

        game_def = game_def.join(
            score_df,
            on=[
                "game_id",
                "defteam",
            ],
            how="left",
        )

    else:
        game_def = (
            game_def.with_columns(
                pl.lit(
                    24.0
                ).alias(
                    "points_allowed"
                )
            )
        )

    game_def = game_def.with_columns(
        pl.col(
            "points_allowed"
        )
        .cast(
            pl.Float64,
            strict=False,
        )
        .fill_null(
            24.0
        )
    )

    # ---------------------------------------------------------
    # Season defensive averages
    # ---------------------------------------------------------

    season_def = (
        game_def
        .group_by(
            "defteam"
        )
        .agg(
            pl.col(
                "game_id"
            ).n_unique().alias(
                "games"
            ),

            pl.col(
                "sacks"
            ).mean().alias(
                "sacks_per_game"
            ),

            pl.col(
                "pressure_proxy"
            ).mean().alias(
                "pressure_rate"
            ),

            pl.col(
                "takeaways"
            ).mean().alias(
                "takeaways_per_game"
            ),

            pl.col(
                "defensive_tds"
            ).mean().alias(
                "defensive_tds_per_game"
            ),

            pl.col(
                "points_allowed"
            ).mean().alias(
                "points_allowed_per_game"
            ),

            pl.col(
                "opponent_yards"
            ).mean().alias(
                "yards_allowed_per_game"
            ),
        )
    )

    # ---------------------------------------------------------
    # Recover offense associated with each defense/game
    # ---------------------------------------------------------

    offense_lookup = (
        defense_pbp
        .filter(
            pl.col(
                "posteam"
            ).is_not_null()
        )
        .group_by(
            [
                "game_id",
                "defteam",
            ]
        )
        .agg(
            pl.col(
                "posteam"
            )
            .mode()
            .first()
            .alias(
                "posteam"
            )
        )
    )

    offense_game = (
        game_def.join(
            offense_lookup,
            on=[
                "game_id",
                "defteam",
            ],
            how="left",
        )
        .filter(
            pl.col(
                "posteam"
            ).is_not_null()
        )
        .with_columns(
            pl.col(
                "points_allowed"
            ).alias(
                "points_scored"
            )
        )
    )

    # ---------------------------------------------------------
    # Prior-season offensive vulnerability
    # ---------------------------------------------------------

    offense = (
        offense_game
        .group_by(
            "posteam"
        )
        .agg(
            pl.col(
                "sacks"
            ).mean().alias(
                "sacks_allowed_pg"
            ),

            pl.col(
                "takeaways"
            ).mean().alias(
                "turnovers_pg"
            ),

            pl.col(
                "points_scored"
            ).mean().alias(
                "points_scored_pg"
            ),
        )
    )

    sack_z = _z_score_map(
        offense,
        key="posteam",
        value="sacks_allowed_pg",
    )

    turnover_z = _z_score_map(
        offense,
        key="posteam",
        value="turnovers_pg",
    )

    points_z = _z_score_map(
        offense,
        key="posteam",
        value="points_scored_pg",
    )

    vulnerability: dict[
        str,
        float,
    ] = {}

    for row in offense.iter_rows(
        named=True,
    ):
        team = row["posteam"]

        vulnerability[team] = (
            0.45
            * sack_z.get(
                team,
                0.0,
            )
            +
            0.35
            * turnover_z.get(
                team,
                0.0,
            )
            -
            0.20
            * points_z.get(
                team,
                0.0,
            )
        )

    # ---------------------------------------------------------
    # Target-season schedule difficulty
    # ---------------------------------------------------------

    matchup_rows: list[dict] = []

    for game in schedules.iter_rows(
        named=True,
    ):
        week = game.get(
            "week"
        )

        home = game.get(
            "home_team"
        )

        away = game.get(
            "away_team"
        )

        if (
            week is None
            or home is None
            or away is None
        ):
            continue

        matchup_rows.append(
            {
                "team": home,
                "opponent": away,
                "week": int(week),
                "score": float(
                    vulnerability.get(
                        away,
                        0.0,
                    )
                ),
            }
        )

        matchup_rows.append(
            {
                "team": away,
                "opponent": home,
                "week": int(week),
                "score": float(
                    vulnerability.get(
                        home,
                        0.0,
                    )
                ),
            }
        )

    schedule_scores: dict[
        str,
        tuple[
            float,
            float,
        ],
    ] = {}

    if matchup_rows:
        matchups = pl.DataFrame(
            matchup_rows
        )

        schedule_summary = (
            matchups
            .group_by(
                "team"
            )
            .agg(
                pl.when(
                    pl.col(
                        "week"
                    )
                    <= early_weeks
                )
                .then(
                    pl.col(
                        "score"
                    )
                )
                .otherwise(
                    None
                )
                .mean()
                .alias(
                    "early"
                ),

                pl.col(
                    "score"
                ).mean().alias(
                    "full"
                ),
            )
        )

        for row in (
            schedule_summary.iter_rows(
                named=True
            )
        ):
            schedule_scores[
                row["team"]
            ] = (
                float(
                    row["early"]
                    or 0.0
                ),
                float(
                    row["full"]
                    or 0.0
                ),
            )

    # ---------------------------------------------------------
    # Final DefenseStats objects
    # ---------------------------------------------------------

    result: list[
        DefenseStats
    ] = []

    for row in season_def.iter_rows(
        named=True,
    ):
        team = _abbr(
            row["defteam"]
        )

        if (
            team
            not in ESPN_TEAM_ID_BY_ABBR
        ):
            continue

        early_schedule, full_schedule = (
            schedule_scores.get(
                team,
                (
                    0.0,
                    0.0,
                ),
            )
        )

        result.append(
            DefenseStats(
                team_id=(
                    ESPN_TEAM_ID_BY_ABBR[
                        team
                    ]
                ),
                team=team,

                sacks_per_game=float(
                    row[
                        "sacks_per_game"
                    ]
                    or 0.0
                ),

                pressure_rate=float(
                    row[
                        "pressure_rate"
                    ]
                    or 0.0
                ),

                takeaways_per_game=float(
                    row[
                        "takeaways_per_game"
                    ]
                    or 0.0
                ),

                defensive_tds_per_game=float(
                    row[
                        "defensive_tds_per_game"
                    ]
                    or 0.0
                ),

                points_allowed_per_game=float(
                    row[
                        "points_allowed_per_game"
                    ]
                    or 24.0
                ),

                yards_allowed_per_game=float(
                    row[
                        "yards_allowed_per_game"
                    ]
                    or 350.0
                ),

                early_schedule_score=(
                    early_schedule
                ),

                season_schedule_score=(
                    full_schedule
                ),
            )
        )

    return result