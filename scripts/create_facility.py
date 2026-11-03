"""Define a facility straight against the database.

The bootstrap path: ``POST /v1/facilities`` needs an existing ``admin`` key, and
minting a key with a facility grant needs the facility to exist, so the first facility
has to be created here. Needs nothing but ``CANOPY_DATABASE_URL``.

``slug`` is immutable — ``?facility=`` filters and key grants reference it, so there is
no rename; fixing a typo means delete and recreate. Slugs are lowercase alphanumerics
with internal hyphens (``lcls``, ``lcls-sc``); ``LCLS`` is rejected, not lowercased.

Run::

    uv run python scripts/create_facility.py lcls --name "LCLS"
    uv run python scripts/create_facility.py lcls-sc --name "LCLS-II SC" --parent lcls

    # In a container:
    docker exec canopy-<instance>-api python scripts/create_facility.py lcls --name LCLS
"""

import sys
import asyncio
import argparse

from pydantic import ValidationError

from canopy.core.db import session_scope, dispose_engine
from canopy.core.errors import CanopyError
from canopy.core.facilities.schemas import FacilityCreate
from canopy.core.facilities.service import FacilityService


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Canopy facility.")
    parser.add_argument("slug", help="Immutable identifier used in API key grants and ?facility= filters")
    parser.add_argument("--name", required=True, help="Human-readable display name")
    parser.add_argument("--description", help="Optional longer description")
    parser.add_argument(
        "--parent",
        metavar="SLUG",
        help="Slug of the parent facility. A grant on the parent also covers this one.",
    )
    args = parser.parse_args()

    try:
        data = FacilityCreate(
            slug=args.slug,
            name=args.name,
            description=args.description,
            parent=args.parent,
        )
    except ValidationError as exc:
        # Reported like a CanopyError, not as a traceback — a mistyped slug is an
        # operator error, not a bug.
        print(f"error: {'; '.join(e['msg'] for e in exc.errors())}", file=sys.stderr)
        return 2

    try:
        async with session_scope() as session:
            facility = await FacilityService(session).create(data)
            await session.commit()
    except CanopyError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    finally:
        await dispose_engine()

    print("Facility created.")
    print(f"  Slug:        {facility.slug}")
    print(f"  ID:          {facility.id}")
    print(f"  Name:        {facility.name}")
    print(f"  Description: {facility.description or '(none)'}")
    print(f"  Parent:      {args.parent or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
