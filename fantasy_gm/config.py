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


@lru_cache
def get_settings() -> Settings:
    return Settings()
