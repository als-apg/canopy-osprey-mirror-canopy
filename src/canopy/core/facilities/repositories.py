"""Facility repository: hierarchy CRUD + descendant-set resolution.

Owns no notion of "is this facility in use" — references live in other modules'
schemas, which this repository can't reach without a cross-schema query. See
``service.py``'s ``_in_use_checkers`` hook for how that check is composed back in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import BaseRepository
from canopy.core.facilities.models import Facility


class FacilityRepository(BaseRepository[Facility]):
    """Repository for Facility operations, including hierarchy resolution."""

    def __init__(self, session: AsyncSession):
        super().__init__(Facility, session)

    async def get_by_slug(self, slug: str) -> Facility | None:
        result = await self.session.execute(select(Facility).where(Facility.slug == slug))
        return result.scalar_one_or_none()

    async def get_children(self, facility_id: str) -> list[Facility]:
        result = await self.session.execute(select(Facility).where(Facility.parent_id == facility_id))
        return list(result.scalars().all())

    async def existing_slugs(self, slugs: list[str]) -> set[str]:
        """The subset of ``slugs`` that exist, in one query."""
        if not slugs:
            return set()
        result = await self.session.execute(select(Facility.slug).where(Facility.slug.in_(slugs)))
        return {row[0] for row in result.all()}

    async def resolve_facility_scope(self, slug: str) -> set[str] | None:
        """Slug -> {facility_id, all descendant facility_ids}. ``None`` if the slug
        is unknown — distinct from ``set()``, which means "known slug, no facilities
        currently match" (not reachable for a single root, but kept for symmetry with
        callers that intersect multiple scopes).

        A recursive CTE over ``parent_id`` starting at the named facility. Plain
        ``union`` (not ``union_all``) — de-duplicating on each step means a cycle
        that got into the table some other way still terminates, instead of looping
        forever on every scope lookup that reaches it.
        """
        root = await self.get_by_slug(slug)
        if root is None:
            return None

        hierarchy = select(Facility.id).where(Facility.id == root.id).cte("facility_scope", recursive=True)
        children = select(Facility.id).join(hierarchy, Facility.parent_id == hierarchy.c.id)
        hierarchy = hierarchy.union(children)

        result = await self.session.execute(select(hierarchy.c.id))
        return {row[0] for row in result.all()}

    async def is_ancestor(self, candidate_id: str, facility_id: str) -> bool:
        """Is ``candidate_id`` ``facility_id`` itself or one of its ancestors?

        Walks ``parent_id`` upward from ``facility_id``. Used to reject reparenting
        a facility under its own descendant — the forward (descendant) direction is
        ``resolve_facility_scope``; this is the same walk run backward, since a
        facility's ancestor set isn't expressible as a single recursive CTE rooted at
        itself the way its descendant set is.
        """
        current_id: str | None = facility_id
        while current_id is not None:
            if current_id == candidate_id:
                return True
            facility = await self.get_by_id(current_id)
            current_id = facility.parent_id if facility else None
        return False
