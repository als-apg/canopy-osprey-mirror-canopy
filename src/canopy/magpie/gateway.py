"""Public surface of the Magpie module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.magpie.*``
is private implementation. Magpie's service is request/session scoped (nothing to
bind), so each call here opens a short-lived read session via ``session_scope`` and
builds the service from it. The app handshake is applied here too.

The frontend talks to magpie over HTTP at ``/magpie/v1``; this file defines the
intended in-process read surface for a future consumer.
"""

from __future__ import annotations

from canopy.core.db import session_scope
from canopy.magpie.schemas.config import ConfigDTO
from canopy.magpie.services.config_service import ConfigService


async def get_config_by_slug(app: str, slug: str) -> ConfigDTO | None:
    """Return a stored config by slug for ``app``, or ``None`` if absent or the app
    doesn't match. Read-only: loaded and mapped inside a short-lived session."""
    async with session_scope() as session:
        return await ConfigService(session).get_by_slug(app, slug)
