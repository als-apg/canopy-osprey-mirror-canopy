"""`build_registry` turns `ElogConfig` into one adapter per app, or a disabled registry."""

import logging

import pytest

from canopy.elog.config import ElogConfig, ElogAppConfig, ElogPlusConfig
from tests.canopy.elog._fakes import FakeElogAdapter
from canopy.elog.adapters.registry import (
    PROVIDER_LOAD_FAILED,
    ElogRegistry,
    build_registry,
)
from canopy.elog.adapters.elog_plus import ElogPlusAdapter

FAKE_PATH = "tests.canopy.elog._fakes:FakeElogAdapter"


def _config(provider: str = "elog_plus", apps: dict | None = None, base_url: str = "http://elog.test") -> ElogConfig:
    return ElogConfig(provider=provider, elog_plus=ElogPlusConfig(base_url=base_url), apps=apps or {})


def test_empty_provider_disables_the_module():
    registry = build_registry(_config(provider="", apps={"squirrel": ElogAppConfig(token="t")}))

    assert registry.enabled is False
    assert registry.apps == []
    assert "CANOPY_ELOG__PROVIDER" in registry.disabled_reason


def test_unknown_provider_disables_the_module_instead_of_failing_startup(caplog):
    caplog.set_level(logging.ERROR, logger="canopy.elog.adapters.registry")

    registry = build_registry(_config(provider="elog-plus", apps={"squirrel": ElogAppConfig(token="t")}))

    assert registry.enabled is False
    assert "Unknown e-log provider 'elog-plus'" in registry.disabled_reason
    assert "elog-plus" in caplog.text


async def test_one_elog_plus_adapter_per_app_with_a_token():
    registry = build_registry(
        _config(
            apps={
                "squirrel": ElogAppConfig(token="squirrel-token"),
                "trace": ElogAppConfig(token="trace-token"),
                "notoken": ElogAppConfig(default_logbook_ids=["lb1"]),
                "blank": ElogAppConfig(token=""),
            }
        )
    )
    try:
        assert registry.enabled is True
        assert registry.provider == "elog_plus"
        assert registry.apps == ["squirrel", "trace"]
        squirrel, trace = registry.get("squirrel"), registry.get("trace")
        assert isinstance(squirrel, ElogPlusAdapter)
        assert squirrel._client.headers["x-vouch-idp-accesstoken"] == "squirrel-token"
        assert trace._client.headers["x-vouch-idp-accesstoken"] == "trace-token"
        assert registry.get("notoken") is None
    finally:
        await registry.close_all()


def test_missing_base_url_disables_the_module_with_its_reason():
    registry = build_registry(_config(base_url="", apps={"squirrel": ElogAppConfig(token="t")}))

    assert registry.enabled is False
    assert "CANOPY_ELOG__ELOG_PLUS__BASE_URL" in registry.disabled_reason


def test_app_names_the_route_could_never_match_are_skipped():
    registry = build_registry(_config(provider=FAKE_PATH, apps={"Bad Name": ElogAppConfig(token="t")}))

    assert registry.enabled is True
    assert registry.apps == []


def test_dotted_path_provider_loads_an_out_of_tree_adapter():
    registry = build_registry(_config(provider=FAKE_PATH, apps={"squirrel": ElogAppConfig(token="t")}))

    assert isinstance(registry.get("squirrel"), FakeElogAdapter)


@pytest.mark.parametrize("provider", ["json:JSONDecoder", "json:loads", "json:__name__"])
def test_dotted_path_to_something_that_is_not_an_adapter_disables_the_module(provider):
    registry = build_registry(_config(provider=provider, apps={"squirrel": ElogAppConfig(token="t")}))

    assert registry.enabled is False
    assert "is not an e-log adapter class" in registry.disabled_reason


def test_adapter_factory_failure_of_any_kind_disables_the_module(caplog):
    caplog.set_level(logging.ERROR, logger="canopy.elog.adapters.registry")

    registry = build_registry(
        _config(provider="tests.canopy.elog._fakes:ExplodingElogAdapter", apps={"squirrel": ElogAppConfig(token="t")})
    )

    assert registry.enabled is False
    # The config route needs no key, so an adapter's own error text stays in the log.
    assert registry.disabled_reason == PROVIDER_LOAD_FAILED
    assert "adapter factory blew up" in caplog.text


@pytest.mark.parametrize(
    ("provider", "error"),
    [
        ("not_a_module:Adapter", "No module named 'not_a_module'"),
        # import_module raises TypeError for a relative path and ValueError for an empty one.
        (".adapter:Adapter", "relative import"),
        (":Adapter", "Empty module name"),
        ("tests.canopy.elog._raises_on_import:Adapter", "adapter module needs SOME_SETTING"),
    ],
)
def test_dotted_path_whose_module_will_not_import_disables_the_module(provider, error, caplog):
    caplog.set_level(logging.ERROR, logger="canopy.elog.adapters.registry")

    registry = build_registry(_config(provider=provider, apps={"squirrel": ElogAppConfig(token="t")}))

    assert registry.enabled is False
    assert registry.disabled_reason.startswith("Cannot import e-log adapter module")
    assert error not in registry.disabled_reason
    assert error in caplog.text


async def test_close_all_closes_every_adapter():
    adapters = {"squirrel": FakeElogAdapter("squirrel"), "trace": FakeElogAdapter("trace")}

    await ElogRegistry("fake", adapters).close_all()

    assert all(a.closed for a in adapters.values())


async def test_health_probe_times_out_per_app():
    slow, fast = FakeElogAdapter("slow"), FakeElogAdapter("fast")
    slow.health_delay_s = 1.0

    health = await ElogRegistry("fake", {"slow": slow, "fast": fast}).list_with_health(timeout_s=0.05)

    assert health["fast"].healthy is True
    assert health["slow"].healthy is False
    assert health["slow"].last_error == "health probe timed out"
