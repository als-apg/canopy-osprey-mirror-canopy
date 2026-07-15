"""Beaver domain exceptions.

The per-module exception handler maps these to ``{"code": ..., "detail": ...}``.
Routes never raise ``HTTPException`` directly; services raise these and the
handler installed by ``register_canopy_error_handler`` shapes the response.
"""

from canopy.core.errors import ErrorCode, CanopyError


class BeaverError(CanopyError):
    """Base for all Beaver errors."""


class WidgetNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.WIDGET_NOT_FOUND


class QtClassNotMapped(BeaverError):
    status_code = 404
    error_code = ErrorCode.QT_CLASS_NOT_MAPPED


class IconNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.ICON_NOT_FOUND


class LegacyScreenNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.LEGACY_SCREEN_NOT_FOUND


class CatalogMisconfigured(BeaverError):
    status_code = 500
    error_code = ErrorCode.CATALOG_MISCONFIGURED


class ScreenSchemaVersionNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.SCREEN_SCHEMA_VERSION_NOT_FOUND
