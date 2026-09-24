"""Where a draft screen can be opened, as returned to the agent in ``url``.

Inside OSPREY the terminal proxies each panel under a per-user prefix, so the
URL is panel-relative (``/panel/<panel_id>/embed/drafts/<id>``) and OSPREY adds
the prefix. Standalone, the agent's browser talks to this server directly, so
the URL is absolute and names the address the server is listening on. That
address lives with the launcher (``--host``/``--port``), not in ``Settings``,
which is why it is passed in.
"""

from canopy_mcp.config import Settings
from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT


def embed_path(draft_id: str) -> str:
    """The viewer's server-relative path for draft ``draft_id`` (the embed route, ``drafts`` category)."""
    return f"/embed/drafts/{draft_id}"


def draft_url(settings: Settings, draft_id: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """Return the URL that opens draft ``draft_id`` in the viewer."""
    path = embed_path(draft_id)
    if settings.panel_id:
        return f"/panel/{settings.panel_id}{path}"
    return f"http://{host}:{port}{path}"
