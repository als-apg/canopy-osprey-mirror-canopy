"""Revoke an API key by ID (soft delete — the audit row stays).

Revocation is not instant across the fleet: each API process caches key lookups,
so a deactivated key keeps working for up to ``CANOPY_API_KEY_CACHE_TTL_S``
(60s by default). Restart the API process if you need it cut off immediately.

Run::

    uv run python scripts/list_keys.py              # find the id
    uv run python scripts/deactivate_key.py <id>
"""

import sys
import asyncio
import argparse

from canopy.core.db import session_scope, dispose_engine
from canopy.core.errors import CanopyError
from canopy.core.keys.service import ApiKeyService


async def main() -> int:
    parser = argparse.ArgumentParser(description="Deactivate a Canopy API key.")
    parser.add_argument("key_id", help="Key UUID, as shown by scripts/list_keys.py")
    args = parser.parse_args()

    try:
        async with session_scope() as session:
            key = await ApiKeyService(session).deactivate_key(args.key_id)
            await session.commit()
    except CanopyError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    finally:
        await dispose_engine()

    print(f"API key deactivated: {key.app_name} ({key.id})")
    print("Cached lookups mean it may still authenticate briefly; restart the API to cut it off now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
