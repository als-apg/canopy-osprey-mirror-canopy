"""`elog_lifespan`: invalid ``CANOPY_ELOG__*`` settings disable e-log instead of stopping Canopy."""

import os

import pytest

from canopy.elog import gateway
from canopy.elog.app import elog_lifespan


@pytest.fixture(autouse=True)
def _clean_elog_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CANOPY_ELOG__"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CANOPY_ELOG__PROVIDER", "elog_plus")
    monkeypatch.setenv("CANOPY_ELOG__ELOG_PLUS__BASE_URL", "http://elog.test")
    monkeypatch.setenv("CANOPY_ELOG__APPS__SQUIRREL__TOKEN", "squirrel-secret")


async def _squirrel_config() -> dict:
    async with elog_lifespan():
        service = gateway.get_service()
        return {
            "config": service.app_config("squirrel"),
            "health": await service.health_summary(),
            "max_attachment_bytes": service.max_attachment_bytes,
        }


async def test_valid_settings_enable_the_app():
    assert (await _squirrel_config())["config"].enabled is True


@pytest.mark.parametrize(
    "name, value, named",
    [
        ("CANOPY_ELOG__TIMEOUT_S", "fifteen-SENSITIVE", "CANOPY_ELOG__TIMEOUT_S"),
        ("CANOPY_ELOG__APPS__SQUIRREL__DEFAULT_LOGBOOK_IDS", '{"id": "SENSITIVE"}', "DEFAULT_LOGBOOK_IDS"),
        # Not JSON at all: pydantic-settings fails before validation, naming only the field.
        ("CANOPY_ELOG__APPS__SQUIRREL__DEFAULT_LOGBOOK_IDS", "[SENSITIVE", '"apps"'),
    ],
)
async def test_unparseable_settings_disable_e_log_with_a_reason_that_names_no_values(monkeypatch, name, value, named):
    monkeypatch.setenv(name, value)

    result = await _squirrel_config()

    config = result["config"]
    assert config.enabled is False
    assert config.provider is None
    assert config.reason.startswith("Invalid e-log settings")
    assert named in config.reason
    assert "SENSITIVE" not in config.reason
    assert "squirrel-secret" not in config.reason
    assert result["health"].status == "disabled"
    assert result["health"].reason == config.reason
    # The service runs on defaults, so uploads keep a sane limit.
    assert result["max_attachment_bytes"] == 20 * 1024 * 1024


async def test_gateway_is_unbound_after_shutdown():
    await _squirrel_config()

    with pytest.raises(RuntimeError):
        gateway.get_service()
