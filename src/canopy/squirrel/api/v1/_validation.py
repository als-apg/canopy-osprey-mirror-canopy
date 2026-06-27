"""Shared id validation for Squirrel's API routers.

Path ids are string UUIDs (``UUID(as_uuid=False)`` primary keys). A malformed id must
become a clean 404 rather than an asyncpg encode error (500), so routers gate on
``is_uuid`` before touching the database.
"""

from uuid import UUID


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
