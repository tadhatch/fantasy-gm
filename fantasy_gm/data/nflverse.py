from __future__ import annotations

from functools import lru_cache

import nflreadpy as nfl
import polars as pl


@lru_cache(maxsize=1)
def load_player_ids() -> pl.DataFrame:
    """Cross-platform player identifiers, including ESPN and FantasyPros IDs."""
    return nfl.load_ff_playerids()


@lru_cache(maxsize=1)
def load_draft_rankings() -> pl.DataFrame:
    """Latest FantasyPros expert-consensus preseason rankings."""
    return nfl.load_ff_rankings(type="draft")


@lru_cache(maxsize=2)
def load_depth_chart(season: int) -> pl.DataFrame:
    """Current season depth chart. Used by the relationship graph later."""
    return nfl.load_depth_charts(season)
