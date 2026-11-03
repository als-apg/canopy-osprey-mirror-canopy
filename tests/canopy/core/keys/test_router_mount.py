"""Where ``/v1/api-keys`` and ``/v1/facilities`` are mounted: on an install with the database key tier.

The env keys sit in front of the database whenever the install has it. This
needs SQLAlchemy installed, so it sits beside the key-store tests; it builds the
apps without opening a database connection.
"""

import canopy.main
from canopy.core import security
from canopy.core.config import Settings
from canopy.core.keys.registry import ChainedKeyRegistry


def _management_paths(app) -> list[str]:
    paths = []
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            # Newer FastAPI keeps each include_router() as one entry with no path of its own.
            prefix = route.include_context.prefix
            paths.extend(prefix + inner.path for inner in route.original_router.routes)
        else:
            paths.append(getattr(route, "path", ""))
    return [path for path in paths if path.startswith(("/v1/api-keys", "/v1/facilities"))]


def _layers(registry) -> list[type]:
    return [type(layer) for layer in registry._registries]


def test_the_database_tier_mounts_key_and_facility_management():
    app = canopy.main.build_app(("hare",))

    paths = _management_paths(app)
    assert any(path.startswith("/v1/api-keys") for path in paths)
    assert any(path.startswith("/v1/facilities") for path in paths)


def test_without_the_database_tier_nothing_is_mounted(monkeypatch):
    monkeypatch.setattr(canopy.main, "has_database_tier", lambda: False)

    assert _management_paths(canopy.main.build_app(("hare",))) == []


def test_env_keys_are_the_static_tier_in_front_of_the_database():
    registry = security.build_key_registry(Settings(api_keys="ops:admin+read@core"))

    assert isinstance(registry, ChainedKeyRegistry)
    assert _layers(registry)[0] is security.EnvKeyRegistry
