"""E-log domain exceptions.

Services and adapters raise these; the per-module exception handler
(registered via ``register_canopy_error_handler``) maps them to
``{"code": ..., "detail": ...}``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class ElogError(CanopyError):
    """Base for all e-log errors."""


class InvalidAppName(ElogError):
    """The ``{app}`` path segment is not a valid app name."""

    status_code = 400
    error_code = ErrorCode.INVALID_APP_NAME


class ElogAppNotFound(ElogError):
    """No e-log token is configured for this app."""

    status_code = 404
    error_code = ErrorCode.ELOG_APP_NOT_FOUND


class ElogNotConfigured(ElogError):
    """No usable e-log provider is configured on this instance."""

    status_code = 503
    error_code = ErrorCode.ELOG_NOT_CONFIGURED


class ElogUpstreamError(ElogError):
    """The e-log backend rejected the call, was unreachable, or answered garbage."""

    status_code = 502
    error_code = ErrorCode.ELOG_UPSTREAM_ERROR


class ElogUpstreamTimeout(ElogError):
    status_code = 504
    error_code = ErrorCode.ELOG_UPSTREAM_TIMEOUT


class ElogNotSupported(ElogError):
    """The configured provider has no equivalent of the requested operation."""

    status_code = 501
    error_code = ErrorCode.ELOG_NOT_SUPPORTED


class ElogAttachmentTooLarge(ElogError):
    """The upload exceeds ``ElogConfig.max_attachment_bytes``."""

    status_code = 413
    error_code = ErrorCode.ELOG_ATTACHMENT_TOO_LARGE
