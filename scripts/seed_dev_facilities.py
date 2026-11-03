"""Seed a development database with the SLAC facility hierarchy.

**Dev and compose only.** Not wired into container startup: each deployment instance
has its own database with its own hierarchy, so there is no single baseline to seed,
and re-seeding on boot would fight facilities edited through the API. Migrations
already follow the same explicit-step convention.

Idempotent — an already-defined slug is reported and skipped.

Run::

    uv run python scripts/seed_dev_facilities.py
"""

import asyncio

from canopy.core.db import session_scope, dispose_engine
from canopy.core.errors import FacilitySlugConflict
from canopy.core.facilities.schemas import FacilityCreate
from canopy.core.facilities.service import FacilityService

# Parents before children — `create` resolves `parent` by slug, so a child whose
# parent hasn't been created yet would fail with FacilityNotFound.
_FACILITIES = [
    FacilityCreate(slug="lcls", name="LCLS"),
    FacilityCreate(slug="lcls-nc", name="LCLS Normal Conducting", parent="lcls"),
    FacilityCreate(slug="lcls-sc", name="LCLS-II Superconducting", parent="lcls"),
    FacilityCreate(slug="facet", name="FACET-II"),
    FacilityCreate(slug="asta", name="ASTA"),
]


async def main() -> int:
    try:
        async with session_scope() as session:
            service = FacilityService(session)
            for data in _FACILITIES:
                try:
                    await service.create(data)
                except FacilitySlugConflict:
                    # If the conflict surfaced as an IntegrityError on flush rather
                    # than the pre-check, the session is unusable until rolled back.
                    await session.rollback()
                    print(f"  {data.slug}: already exists, skipped")
                    continue
                # Committed per facility, not once at the end: a child resolves its
                # parent by slug, and a later failure must not roll back the parents
                # that already succeeded.
                await session.commit()
                print(f"  {data.slug}: created")
    finally:
        await dispose_engine()

    print("Done. Verify with scripts/list_facilities.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
