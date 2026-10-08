"""The canonical set of module scopes an API key can be restricted to.

Mirrors the mount points in ``canopy.main`` plus ``core`` for the parent app's
own routes (``/health``, ``/v1/api-keys``). Hardcoded rather than derived from
``main.py`` at import time — ``main`` imports ``canopy.core.security``, so the
reverse import would be circular — but ``tests/canopy/core/test_modules.py``
asserts this set equals the actual mounts, so an eleventh sub-app cannot be
added without either updating this set or failing CI.
"""

WILDCARD = "*"

JOBS = "jobs"
"""Any module-scoped key is implicitly allowed here: a job's status poll
(``GET /jobs/v1/{id}``) carries no data beyond what the enqueuing route
(squirrel, converter, ...) already returned, so gating it behind an explicit
``+jobs`` would just break polling for every module that hands out job ids."""

MODULES: frozenset[str] = frozenset(
    {
        "core",
        "mole",
        "hare",
        "raccoon",
        "vole",
        "fox",
        "squirrel",
        "magpie",
        "jobs",
        "converter",
        "analytics",
    }
)


def split_modules(modules: set[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Partition a set of module names into (recognized, unrecognized).

    Shared by ``EnvKeyRegistry`` and the key-management service, which parse
    the same ``@module+module`` shape but disagree on what to do with an
    unrecognized name: the env layer warns and drops it (a typo in a static
    key must not take an otherwise-good entry down), the service layer raises
    (a typo in an HTTP/CLI request is a mistake the caller can fix on the
    spot). That difference stays with each caller; this only does the
    classification. ``WILDCARD`` counts as recognized, and its presence
    collapses the recognized set down to just ``{WILDCARD}``.
    """
    unknown = frozenset(modules) - MODULES - {WILDCARD}
    known = frozenset(modules) - unknown
    if WILDCARD in known:
        known = frozenset({WILDCARD})
    return known, unknown


def tag_module(app, module: str) -> None:
    """Attach ``module`` to an ASGI app instance, read back via ``request.app.state``
    / ``websocket.app.state`` in ``canopy.core.security``.

    Deliberately not derived from the URL: a mounted sub-app's own
    ``scope["root_path"]`` is *proxy prefix + mount path* (Starlette appends the
    mount to whatever root_path it was already given), so behind a path-prefixed
    ingress (``uvicorn --root-path /canopy``) parsing it back apart is fragile —
    it would have returned ``canopy/hare`` instead of ``hare``. Each app carries
    its own module tag instead, independent of how anything in front of it
    rewrites the path.
    """
    app.state.canopy_module = module


def module_for_app(app) -> str:
    """The module tag ``tag_module`` attached to this ASGI app instance."""
    return app.state.canopy_module
