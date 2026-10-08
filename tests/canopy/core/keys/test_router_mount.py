"""Where ``/v1/api-keys`` is mounted: only on the env + database chain.

The env-registry case lives in ``tests/canopy/test_closed_meta.py`` and the
keys-file case in ``tests/canopy/core/test_keys_file.py`` (both pins files). This
one needs SQLAlchemy installed, so it sits beside origin's key-store tests; it
builds the apps without opening a database connection.
"""

import pytest

import canopy.main
from canopy.core import security
from canopy.core.config import Settings
from canopy.core.keys.registry import ChainedKeyRegistry


def _api_key_paths(app) -> list[str]:
    paths = []
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            # Newer FastAPI keeps each include_router() as one entry with no path of its own.
            prefix = route.include_context.prefix
            paths.extend(prefix + inner.path for inner in route.original_router.routes)
        else:
            paths.append(getattr(route, "path", ""))
    return [path for path in paths if path.startswith("/v1/api-keys")]


@pytest.fixture
def build_with(monkeypatch):
    """Build the app under the given settings, then put the previous registry back."""
    previous = security.get_registry()

    def build(settings: Settings):
        monkeypatch.setattr(security, "get_settings", lambda: settings)
        return canopy.main.build_app(("hare",), closed_meta=False)

    yield build
    security.bind_key_registry(previous)


def test_the_database_chain_mounts_key_management(build_with):
    app = build_with(Settings(api_keys="ops:admin+read@core", api_keys_file=None))

    assert isinstance(security.get_registry(), ChainedKeyRegistry)
    assert _api_key_paths(app)


def test_a_keys_file_mounts_no_key_management_even_with_sqlalchemy_installed(build_with, tmp_path):
    path = tmp_path / "data.keys"
    path.write_text("ops:read+admin@core\n")

    app = build_with(Settings(api_keys="ops:admin+read@core", api_keys_file=str(path)))

    assert isinstance(security.get_registry(), security.FileKeyRegistry)
    assert _api_key_paths(app) == []
