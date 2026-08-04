"""Lightweight path-param validation for the magpie API.

Rejecting malformed slugs/app names with a clean domain error keeps behavior
uniform with squirrel's ``is_uuid`` guard and avoids a needless DB round-trip on
obvious garbage.
"""

import re
from uuid import UUID

# base62, up to the column width (see Config.slug).
_SLUG_RE = re.compile(r"^[0-9A-Za-z]{1,16}$")
# URL/index-friendly app names.
_APP_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def is_slug(value: str) -> bool:
    return bool(_SLUG_RE.fullmatch(value))


def is_app(value: str) -> bool:
    return bool(_APP_RE.fullmatch(value))


def is_uuid(value: str) -> bool:
    """The continuation token is a row ``id``; an unparseable one must not reach asyncpg."""
    try:
        UUID(value)
    except ValueError:
        return False
    return True
