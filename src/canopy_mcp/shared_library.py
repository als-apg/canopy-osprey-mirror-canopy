"""The shared screen library, mounted onto the app: saved views and collections.

:func:`mount_library` is the one place the app reaches into the library modules.
It adds the routes ``/views`` (:mod:`canopy_mcp.views`), ``/collections`` and
``/collections-state`` (:mod:`canopy_mcp.collections`), behind the same guard as
every other route, and names them in the viewer's ``config.json``
(:func:`library_config`). It puts the stores on ``app.state`` (``views``,
``collections``, ``facility_collections``) and returns the library's lifespan hook.

These routes carry no authentication of their own. Without
``CANOPY_ACCESS_TOKEN_FILE`` the only guard on their writes is ``Sec-Fetch-Site``,
which a non-browser client simply leaves out, so such a server is for a
single-user host; on a shared host, set the access token.
"""

from __future__ import annotations

from typing import Any
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable, AsyncIterator

from starlette.applications import Starlette

from canopy_mcp.views import VIEWS_PATH, ViewStore, build_view_routes
from canopy_mcp.config import Settings
from canopy_mcp.collections import (
    COLLECTIONS_PATH,
    CollectionStore,
    FacilityCollections,
    build_collection_routes,
)

__all__ = ["library_config", "mount_library"]


def library_config(settings: Settings) -> dict[str, Any]:
    """The library's fields of the viewer's ``config.json``.

    ``views`` and ``collections`` always name their APIs (the personal collection
    store always exists, by default ``<drafts_dir>/collections``).
    """
    return {"views": {"path": VIEWS_PATH}, "collections": {"path": COLLECTIONS_PATH}}


def mount_library(app: Starlette, settings: Settings) -> Callable[[Starlette], AbstractAsyncContextManager[Any]]:
    """Add the shared library's routes and stores to ``app``; returns its lifespan hook."""
    views = ViewStore(settings.drafts_dir, views_dir=settings.views_dir)
    facility = (
        FacilityCollections(settings.facility_collections_dir)
        if settings.facility_collections_dir is not None
        else None
    )
    collections = CollectionStore(settings.personal_collections_dir, facility=facility)
    app.router.routes.extend(
        [
            *build_view_routes(views),
            *build_collection_routes(collections, facility),
        ]
    )
    app.state.views = views
    app.state.collections = collections
    app.state.facility_collections = facility
    app.state.viewer_config_fields = library_config(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield

    return lifespan
