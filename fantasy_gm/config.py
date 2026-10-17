import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    espn_league_id: int = Field(alias="ESPN_LEAGUE_ID")
    espn_team_id: int = Field(alias="ESPN_TEAM_ID")
    espn_season: int = Field(default=2026, alias="ESPN_SEASON")
    espn_swid: str = Field(alias="ESPN_SWID")
    espn_s2: str = Field(alias="ESPN_S2")
    espn_poll_seconds: float = Field(default=2.0, alias="ESPN_POLL_SECONDS")

    favorite_team: str | None = Field(
        default=None,
        alias="FANTASY_GM_FAVORITE_TEAM",
    )

    fandom_weight: float = Field(
        default=1.5,
        alias="FANTASY_GM_FANDOM_WEIGHT",
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()


# Env helpers for settings read outside Settings. Railway workers get
# variables as `${{ shared.NAME }}` references (railway/services.py), so
# one that isn't defined in the project arrives as an empty string --
# treat blank the same as unset, never as a parse error.

def env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
