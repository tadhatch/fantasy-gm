from fantasy_gm.config import env_bool, env_float, env_int
from fantasy_gm.lineup.availability import AvailabilitySettings


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    # Railway hands workers an empty string for an undefined shared
    # variable reference -- that must mean "use the default", not crash.
    monkeypatch.setenv("FANTASY_GM_INACTIVES_LEAD_MINUTES", "")
    monkeypatch.setenv("FANTASY_GM_AVAILABILITY_MAX_AGE_MINUTES", " ")
    monkeypatch.setenv("FANTASY_GM_LINEUP_CONFIRM", "")
    monkeypatch.setenv("FANTASY_GM_LINEUP_MAX_REPAIR_ATTEMPTS", "")

    settings = AvailabilitySettings.from_env()
    assert settings.inactives_lead.total_seconds() == 90 * 60
    assert settings.max_data_age.total_seconds() == 20 * 60
    assert env_bool("FANTASY_GM_LINEUP_CONFIRM", False) is False
    assert env_int("FANTASY_GM_LINEUP_MAX_REPAIR_ATTEMPTS", 3) == 3


def test_set_env_values_are_used(monkeypatch):
    monkeypatch.setenv("X_FLOAT", "12.5")
    monkeypatch.setenv("X_BOOL", "Yes")
    assert env_float("X_FLOAT", 1.0) == 12.5
    assert env_bool("X_BOOL", False) is True
