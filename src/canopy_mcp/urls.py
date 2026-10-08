"""Where a screen can be opened, as returned to the agent in ``url``.

Inside OSPREY the terminal proxies each panel under a per-user prefix, so the
URL is panel-relative (``/panel/<panel_id>/embed/drafts/<id>``) and OSPREY adds
the prefix. Standalone, the agent's browser talks to this server directly, so
the URL is absolute and names the address the server is listening on. That
address lives with the launcher (``--host``/``--port``), not in ``Settings``,
which is why it is passed in.

The embed route names the manifest category before the id. With a library
configured (``CANOPY_LIBRARY_DIR``) the tool id ``library/<id>`` is the library
screen ``<id>``, opened at ``/embed/library/<id>``; every other id, and every id
when no library is configured, is a draft at ``/embed/drafts/<id>``.
"""

from canopy_mcp.store import DRAFTS, LIBRARY
from canopy_mcp.config import Settings
from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT


def split_id(settings: Settings, draft_id: str) -> tuple[str, str]:
    """``(category, local id)`` for the id a tool was given.

    ``library/<id>`` is ``("library", "<id>")`` when a library is configured;
    anything else is ``("drafts", draft_id)``. Nothing is validated here.
    """
    if settings.library_dir is not None and isinstance(draft_id, str):
        first, sep, rest = draft_id.partition("/")
        if first == LIBRARY and sep:
            return LIBRARY, rest
    return DRAFTS, draft_id


def embed_path(draft_id: str, category: str = DRAFTS) -> str:
    """The viewer's server-relative path for screen ``draft_id`` of ``category`` (the embed route)."""
    return f"/embed/{category}/{draft_id}"


def draft_url(settings: Settings, draft_id: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """Return the URL that opens the screen a tool knows as ``draft_id`` in the viewer."""
    category, local_id = split_id(settings, draft_id)
    path = embed_path(local_id, category)
    if settings.panel_id:
        return f"/panel/{settings.panel_id}{path}"
    return f"http://{host}:{port}{path}"
