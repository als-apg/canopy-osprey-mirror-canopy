"""The shared screen library, mounted onto the app: saved views, collections and the workspace hub.

:func:`mount_library` is the one place the app reaches into the library modules.
It adds the routes ``/views`` (:mod:`canopy_mcp.views`), ``/collections`` and
``/collections-state`` (:mod:`canopy_mcp.collections`) and, with
``CANOPY_WORKSPACE`` on, ``/workspace/...`` (:mod:`canopy_mcp.workspace_routes`),
behind the same guard as every other route, and names them in the viewer's
``config.json`` (:func:`library_config`). It puts the stores on ``app.state``
(``views``, ``collections``, ``facility_collections``) and returns the lifespan
hook that, with the workspace on, closes the hub on the server's exit signal and,
on every shutdown, resets the process hub, which wakes whatever still waits on it.

These routes carry no authentication of their own. Without
``CANOPY_ACCESS_TOKEN_FILE`` the only guard on their writes is ``Sec-Fetch-Site``,
which a non-browser client simply leaves out, so such a server is for a
single-user host; on a shared host, set the access token.
"""

from __future__ import annotations

from typing import Any
from contextlib import AsyncExitStack, AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable, AsyncIterator

from starlette.applications import Starlette

from canopy_mcp.views import VIEWS_PATH, ViewStore, build_view_routes
from canopy_mcp.config import Settings
from canopy_mcp.workspace import PROTOCOL, reset_hub
from canopy_mcp.collections import (
    COLLECTIONS_PATH,
    CollectionStore,
    FacilityCollections,
    build_collection_routes,
)
from canopy_mcp.workspace_routes import (
    WORKSPACE_PATH,
    exit_watch,
    build_workspace_routes,
)

__all__ = ["library_config", "mount_library"]


def library_config(settings: Settings) -> dict[str, Any]:
    """The library's fields of the viewer's ``config.json``.

    ``views`` and ``collections`` always name their APIs (the personal collection
    store always exists, by default ``<drafts_dir>/collections``); ``workspace``
    names the hub, with its protocol, only with ``CANOPY_WORKSPACE`` on.
    """
    fields: dict[str, Any] = {"views": {"path": VIEWS_PATH}, "collections": {"path": COLLECTIONS_PATH}}
    if settings.workspace:
        fields["workspace"] = {"path": WORKSPACE_PATH, "protocol": PROTOCOL}
    return fields


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
            *(build_workspace_routes() if settings.workspace else []),
        ]
    )
    app.state.views = views
    app.state.collections = collections
    app.state.facility_collections = facility
    app.state.viewer_config_fields = library_config(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if settings.workspace:
                await stack.enter_async_context(exit_watch())
            try:
                yield
            finally:
                reset_hub()

    return lifespan
