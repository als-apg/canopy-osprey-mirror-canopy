"""Magpie domain exceptions.

Services raise these; the per-module exception handler (registered on the magpie
sub-app) maps them to the uniform ``{"code", "detail"}`` JSON. Services never raise
``HTTPException`` directly — mirrors the other Canopy modules' ``errors.py``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class MagpieError(CanopyError):
    """Base for all magpie errors."""


class ConfigNotFound(MagpieError):
    status_code = 404
    error_code = ErrorCode.CONFIG_NOT_FOUND


class ConfigSlugConflict(MagpieError):
    status_code = 409
    error_code = ErrorCode.CONFIG_SLUG_CONFLICT
