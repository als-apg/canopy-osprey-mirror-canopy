"""List defined facilities and their hierarchy.

Answers "what slugs exist?" — the question to settle before minting a key with
``create_key.py -f <slug>``, since an unknown slug is rejected at mint time.
``parent`` is shown as a slug, since that's what the other CLI tools take.

Run::

    uv run python scripts/list_facilities.py
"""

import asyncio
import argparse

from canopy.core.db import session_scope, dispose_engine
from canopy.core.facilities.service import FacilityService

_HEADERS = ("slug", "name", "parent", "description", "id")


async def main() -> int:
    argparse.ArgumentParser(description="List Canopy facilities.").parse_args()

    try:
        async with session_scope() as session:
            facilities = await FacilityService(session).get_all()
    finally:
        await dispose_engine()

    if not facilities:
        print("No facilities. Create one with scripts/create_facility.py.")
        return 0

    slug_by_id = {f.id: f.slug for f in facilities}
    rows = [
        (
            f.slug,
            f.name,
            slug_by_id.get(f.parent_id, "") if f.parent_id else "",
            f.description or "",
            f.id,
        )
        for f in sorted(facilities, key=lambda f: f.slug)
    ]
    widths = [max(len(str(cell)) for cell in column) for column in zip(_HEADERS, *rows)]
    print("  ".join(header.ljust(width) for header, width in zip(_HEADERS, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
