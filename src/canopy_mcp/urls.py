"""Where a screen can be opened, as returned to the agent in ``url``.

Behind an embedding host's proxy (``CANOPY_URL_PREFIX`` set, e.g. ``/panel/canopy``)
the URL is that prefix plus the server path, and the host adds whatever lies in
front of it. Standalone, the agent's browser talks to this server directly, so
the URL is absolute: ``CANOPY_PUBLIC_URL``, else ``CANOPY_SELF_URL`` (which the
launcher fills in from the port it serves), else the default address.

The embed route names the manifest category before the id. With a library
configured (``CANOPY_LIBRARY_DIR``) the tool id ``library/<id>`` is the library
screen ``<id>``, opened at ``/embed/library/<id>``; every other id, and every id
when no library is configured, is a draft at ``/embed/drafts/<id>``.
"""

from canopy_mcp.store import DRAFTS, LIBRARY
from canopy_mcp.config import Settings


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


def server_url(settings: Settings, path: str) -> str:
    """The URL the agent opens for the server-relative ``path`` (which starts with ``/``)."""
    prefix = settings.url_prefix
    if prefix:
        return f"{prefix}{path}"
    return f"{settings.screen_base_url}{path}"


def draft_url(settings: Settings, draft_id: str) -> str:
    """The URL that opens the screen a tool knows as ``draft_id`` in the viewer."""
    category, local_id = split_id(settings, draft_id)
    return server_url(settings, embed_path(local_id, category))
