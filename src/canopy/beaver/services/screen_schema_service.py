"""Accessors for the vendored Screen IR JSON Schema documents.

The files under ``canopy.beaver.data.schemas`` are copied byte-for-byte from
the ``@canopy/screen-ir`` package in the ``canopy-screen-builder`` repo, which
is the source of truth for the Screen IR shape. A CI job that byte-compares
this backend copy against the npm-published schema is planned, to catch drift
between the two repos automatically.

Plain functions, no class: unlike ``WidgetService``, there is no catalog to
load and hold — the data is static package data resolved fresh from disk (or
the wheel's zipped resources) on every call.
"""

from importlib.resources import files
from importlib.resources.abc import Traversable

from canopy.beaver.errors import ScreenSchemaVersionNotFound

# Vendored schema filenames follow "screen-ir-{version}.schema.json".
_FILENAME_PREFIX = "screen-ir-"
_FILENAME_SUFFIX = ".schema.json"


def _schemas_dir() -> Traversable:
    return files("canopy.beaver.data") / "schemas"


def _version_sort_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def list_versions() -> list[str]:
    """All Screen IR schema versions vendored in this build, sorted ascending."""
    versions = []
    for entry in _schemas_dir().iterdir():
        name = entry.name
        if name.startswith(_FILENAME_PREFIX) and name.endswith(_FILENAME_SUFFIX):
            versions.append(name.removeprefix(_FILENAME_PREFIX).removesuffix(_FILENAME_SUFFIX))
    return sorted(versions, key=_version_sort_key)


def get_schema_bytes(version: str) -> bytes:
    """Raw bytes of the vendored schema document for ``version``, byte-identical to the source file.

    Looks up ``version`` against the set of versions discovered on disk rather
    than building a path from user input, so a traversal payload (e.g.
    ``../../etc/passwd``) can never resolve to a filesystem entry outside the
    schemas directory.
    """
    schemas_dir = _schemas_dir()
    by_version = {
        entry.name.removeprefix(_FILENAME_PREFIX).removesuffix(_FILENAME_SUFFIX): entry
        for entry in schemas_dir.iterdir()
        if entry.name.startswith(_FILENAME_PREFIX) and entry.name.endswith(_FILENAME_SUFFIX)
    }
    entry = by_version.get(version)
    if entry is None:
        raise ScreenSchemaVersionNotFound(f"Screen IR schema version not found: {version}")
    return entry.read_bytes()
