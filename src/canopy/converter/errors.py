"""Converter domain exceptions.

Services raise these; the per-module exception handler (registered via
``register_canopy_error_handler``) maps them to ``{"code": ..., "detail": ...}``.
Services never import FastAPI or raise ``HTTPException`` directly — that is what
keeps them extractable later without rewriting error sites.
"""

from canopy.core.errors import ErrorCode, CanopyError


class ConverterError(CanopyError):
    """Base for all converter errors."""


class UnsupportedInput(ConverterError):
    """The uploaded screen is not a kind the converter understands."""

    status_code = 415
    error_code = ErrorCode.CONVERTER_UNSUPPORTED_INPUT


class ConversionFailed(ConverterError):
    """The input was a known kind but could not be parsed/built into IR."""

    status_code = 422
    error_code = ErrorCode.CONVERTER_CONVERSION_FAILED


class UploadTooLarge(ConverterError):
    """The uploaded screen exceeds the configured size limit."""

    status_code = 413
    error_code = ErrorCode.CONVERTER_UPLOAD_TOO_LARGE
