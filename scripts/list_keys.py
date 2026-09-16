"""List API keys and their metadata — never their tokens.

``lastUsed`` is what answers "is it safe to deactivate the old key yet?" during a
rotation. It is stamped on a registry cache miss, so it lags live traffic by up to
``CANOPY_API_KEY_CACHE_TTL_S``; treat a timestamp within the last few minutes as
"currently in use".

Run::

    uv run python scripts/list_keys.py
    uv run python scripts/list_keys.py -a          # active only
"""

import asyncio
import argparse

from canopy.core.db import session_scope, dispose_engine
from canopy.core.keys.service import ApiKeyService

_HEADERS = ("id", "app name", "active", "read", "write", "admin", "created", "last used")


async def main() -> int:
    parser = argparse.ArgumentParser(description="List Canopy API keys.")
    parser.add_argument("-a", "--active-only", action="store_true", help="Show only active keys")
    args = parser.parse_args()

    try:
        async with session_scope() as session:
            keys = await ApiKeyService(session).list_keys(active_only=args.active_only)
    finally:
        await dispose_engine()

    if not keys:
        print("No API keys." if not args.active_only else "No active API keys.")
        return 0

    rows = [
        (
            key.id,
            key.app_name,
            str(key.is_active),
            str(key.read_access),
            str(key.write_access),
            str(key.admin_access),
            key.created_at.strftime("%Y-%m-%d %H:%M"),
            key.last_used_at.strftime("%Y-%m-%d %H:%M") if key.last_used_at else "never",
        )
        for key in keys
    ]
    widths = [max(len(str(cell)) for cell in column) for column in zip(_HEADERS, *rows)]
    line = "  ".join(header.ljust(width) for header, width in zip(_HEADERS, widths))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
