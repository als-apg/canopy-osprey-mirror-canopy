"""Public surface of ``core.facilities`` for in-process callers.

Other Canopy modules import only from this file; the rest of
``canopy.core.facilities.*`` is private implementation. Nothing is bound at startup —
each call opens its own short-lived ``session_scope`` and builds the service from it.
"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from canopy.core.db import session_scope
from canopy.core.facilities.schemas import FacilityDTO
from canopy.core.facilities.service import FacilityService
from canopy.core.facilities.service import (
    register_in_use_checker as _register_in_use_checker,
)


async def resolve_facility_scope(slug: str | None) -> set[str] | None:
    """Slug -> {facility_id, all descendant facility_ids}. See ``FacilityService.resolve_facility_scope``."""
    async with session_scope() as session:
        return await FacilityService(session).resolve_facility_scope(slug)


async def resolve_key_facility_scope(slugs: frozenset[str]) -> set[str] | None:
    """A credential's granted slugs -> union of their descendant id sets, or ``None``
    if unrestricted. See ``FacilityService.resolve_key_facility_scope``.

    Resolve this **once per request** (a service-factory dependency is the natural
    place) rather than per service call — every call here opens its own session.
    """
    async with session_scope() as session:
        return await FacilityService(session).resolve_key_facility_scope(slugs)


async def resolve_effective_scope(slug: str | None, key_scope: set[str] | None) -> set[str] | None:
    """Compose a request's ``?facility=`` filter with the calling key's grant.

    Both sides use the same sentinel — ``None`` is "no restriction from this side",
    ``set()`` is "restricted to nothing" — so composing is a plain intersection with
    ``None`` as the identity. An empty result means the query asked for a facility the
    key isn't granted, and the caller returns zero rows rather than everything.
    """
    requested = await resolve_facility_scope(slug)
    if requested is None:
        return key_scope
    return requested if key_scope is None else requested & key_scope


async def get_by_slug(slug: str) -> FacilityDTO | None:
    """Return a facility by slug, or ``None`` if no such facility. Read-only."""
    async with session_scope() as session:
        return await FacilityService(session).get_by_slug(slug)


async def known_slugs(slugs: list[str]) -> set[str]:
    """The subset of ``slugs`` that exist. Used to reject unknown facilities at the
    point a reference to one is *written* (e.g. minting an API key's grant)."""
    async with session_scope() as session:
        return await FacilityService(session).known_slugs(slugs)


def register_in_use_checker(checker: Callable[[str, str], Awaitable[bool]], what: str) -> None:
    """Register an "is this facility still referenced by my data?" check, consulted by
    ``FacilityService.delete``.

    The checker receives ``(facility_id, slug)`` — module data references a facility by
    id, a key grant by slug. ``what`` names what to go unassign, e.g. ``"PVs or
    snapshots"``.
    """
    _register_in_use_checker(checker, what)
