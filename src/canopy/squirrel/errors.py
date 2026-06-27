"""Squirrel domain exceptions.

Services raise these; the per-module exception handler (registered on the squirrel
sub-app) maps them to the uniform ``{"code", "detail"}`` JSON. Services never raise
``HTTPException`` directly — mirrors the other Canopy modules' ``errors.py``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class SquirrelError(CanopyError):
    """Base for all squirrel errors."""


class SnapshotNotFound(SquirrelError):
    status_code = 404
    error_code = ErrorCode.SNAPSHOT_NOT_FOUND


class PvNotFound(SquirrelError):
    status_code = 404
    error_code = ErrorCode.PV_NOT_FOUND


class PvAlreadyExists(SquirrelError):
    status_code = 409
    error_code = ErrorCode.PV_ALREADY_EXISTS


class TagNotFound(SquirrelError):
    status_code = 404
    error_code = ErrorCode.TAG_NOT_FOUND


class TagAlreadyExists(SquirrelError):
    status_code = 409
    error_code = ErrorCode.TAG_ALREADY_EXISTS


class TagGroupNotFound(SquirrelError):
    status_code = 404
    error_code = ErrorCode.TAG_GROUP_NOT_FOUND


class TagGroupAlreadyExists(SquirrelError):
    status_code = 409
    error_code = ErrorCode.TAG_GROUP_ALREADY_EXISTS


class MetaPvNotFound(SquirrelError):
    status_code = 404
    error_code = ErrorCode.META_PV_NOT_FOUND
