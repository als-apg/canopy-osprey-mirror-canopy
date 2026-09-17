"""Core ``Settings``: the log level default and its environment override."""

from canopy.core.config import Settings, get_settings


def test_log_level_defaults_to_info():
    assert Settings().log_level == "INFO"


def test_log_level_can_be_set_explicitly():
    assert Settings(log_level="WARNING").log_level == "WARNING"


def test_log_level_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CANOPY_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    try:
        assert get_settings().log_level == "DEBUG"
    finally:
        get_settings.cache_clear()
