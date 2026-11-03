"""Mint an API key straight against the database.

This is the bootstrap path: the HTTP key-management endpoints require an existing
`admin` key, so the first one has to be created here. It also stays the normal
provisioning tool for facility ops, needing nothing but ``CANOPY_DATABASE_URL``.

The plaintext token is printed **once** and never stored — only its SHA-256 hash
goes to the database. If it is lost, deactivate the key and mint another.

Run::

    uv run python scripts/create_key.py "Trace Display" -r
    uv run python scripts/create_key.py operator-tools -r -w
    uv run python scripts/create_key.py ops -a
    uv run python scripts/create_key.py trace -r -m mole -m fox   # scoped to two modules

    # In a container (local Docker Compose, or a facility instance where the
    # database is only reachable from the running api container):
    docker compose run --rm api python scripts/create_key.py "<app>" -r
"""

import sys
import asyncio
import argparse

from canopy.core.db import session_scope, dispose_engine
from canopy.core.errors import CanopyError
from canopy.core.modules import WILDCARD
from canopy.core.keys.service import ApiKeyService


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Canopy API key.")
    parser.add_argument("app_name", help="Human-readable label for the client class holding the key")
    parser.add_argument("-r", "--read", action="store_true", help="Grant read access")
    parser.add_argument("-w", "--write", action="store_true", help="Grant write access (PV puts, snapshots, configs)")
    parser.add_argument("-a", "--admin", action="store_true", help="Grant admin access (API key management)")
    parser.add_argument(
        "-m",
        "--module",
        action="append",
        dest="modules",
        metavar="MODULE",
        help="Restrict the key to this module (repeatable). Omit for access to every module.",
    )
    parser.add_argument(
        "-f",
        "--facility",
        action="append",
        dest="facilities",
        metavar="FACILITY",
        help="Scope the key's data access to this facility (repeatable). Omit for no facility grants yet.",
    )
    args = parser.parse_args()

    try:
        async with session_scope() as session:
            key = await ApiKeyService(session).create_key(
                args.app_name,
                read=args.read,
                write=args.write,
                admin=args.admin,
                modules=args.modules or [WILDCARD],
                facilities=args.facilities or [],
            )
            await session.commit()
    except CanopyError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    finally:
        await dispose_engine()

    scopes = [
        name for name, on in (("read", key.read_access), ("write", key.write_access), ("admin", key.admin_access)) if on
    ]
    print("API key created.")
    print(f"  App name:  {key.app_name}")
    print(f"  ID:        {key.id}")
    print(f"  Access:    {', '.join(scopes)}")
    print(f"  Modules:   {', '.join(key.modules)}")
    print(f"  Facilities:{' ' + ', '.join(key.facilities) if key.facilities else ' (none)'}")
    print(f"  Created:   {key.created_at}")
    print(f"  Token:     {key.token}")
    print()
    print("Store the token now — it is not recoverable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
