"""The canonical set of module scopes an API key can be restricted to.

Mirrors the mount points in ``canopy.main`` plus ``core`` for the parent app's
own routes (``/health``, ``/v1/api-keys``). Hardcoded rather than derived from
``main.py`` at import time — ``main`` imports ``canopy.core.security``, so the
reverse import would be circular — but ``tests/canopy/core/test_modules.py``
asserts this set equals the actual mounts, so an eleventh sub-app cannot be
added without either updating this set or failing CI.
"""

WILDCARD = "*"

MODULES: frozenset[str] = frozenset(
    {
        "core",
        "mole",
        "hare",
        "raccoon",
        "vole",
        "fox",
        "beaver",
        "squirrel",
        "magpie",
        "jobs",
        "converter",
        "analytics",
    }
)


def module_for_root_path(root_path: str) -> str:
    """Map a request's ``scope["root_path"]`` to its module scope.

    A mounted sub-app's ``root_path`` is its mount point (``"/hare"``); the
    parent app's own routes have an empty ``root_path``, which maps to ``core``.
    Verified to hold for both HTTP and WebSocket scopes.
    """
    return root_path.strip("/") or "core"
