"""Fox domain exceptions.

Services raise these. The per-module exception handler (registered via
``register_canopy_error_handler``) maps them to ``{"code": ..., "detail": ...}``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class FoxError(CanopyError):
    """Base for all Fox formula-engine errors."""


class FormulaParseError(FoxError):
    status_code = 400
    error_code = ErrorCode.FORMULA_PARSE_ERROR


class FormulaUnsafeError(FoxError):
    status_code = 400
    error_code = ErrorCode.FORMULA_UNSAFE


class FormulaInputMissing(FoxError):
    status_code = 422
    error_code = ErrorCode.FORMULA_INPUT_MISSING


class FormulaTypeError(FoxError):
    status_code = 422
    error_code = ErrorCode.FORMULA_TYPE_ERROR


class FormulaDimensionMismatch(FoxError):
    status_code = 422
    error_code = ErrorCode.FORMULA_DIMENSION_MISMATCH


class FormulaEvalTimeout(FoxError):
    status_code = 504
    error_code = ErrorCode.FORMULA_EVAL_TIMEOUT


class FormulaCircularReference(FoxError):
    status_code = 400
    error_code = ErrorCode.FORMULA_CIRCULAR_REFERENCE


class SubscriptionLimitExceeded(FoxError):
    status_code = 429
    error_code = ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED


class InvalidSpec(FoxError):
    """A spec that parses as JSON but breaks a server-side limit (e.g. table caps)."""

    status_code = 400
    error_code = ErrorCode.INVALID_SPEC


class TableLiveOnly(FoxError):
    """A ``table`` was asked for a one-shot evaluation; tables are live-only."""

    status_code = 400
    error_code = ErrorCode.TABLE_LIVE_ONLY


class TableUpdateUnsupported(FoxError):
    """A WS ``update`` to or from a ``table``; tables change by subscribe-new/unsubscribe-old."""

    status_code = 400
    error_code = ErrorCode.TABLE_UPDATE_UNSUPPORTED


TABLE_UPDATE_UNSUPPORTED_MESSAGE = "a table cannot be updated in place: subscribe the new one, unsubscribe the old"


class TableLimit(FoxError):
    """A client holds the maximum number of live table subscriptions."""

    status_code = 429
    error_code = ErrorCode.TABLE_LIMIT


# Codes a live subscription can recover from on its own: the server keeps
# retrying, so the client shows "waiting" rather than ending the subscription.
# Everything else is deterministic for the spec as given (parse, unsafe, type,
# dimension, circular reference, contract rejects) and is never retried.
TRANSIENT_ERROR_CODES = frozenset(
    {
        ErrorCode.FORMULA_INPUT_MISSING,
        ErrorCode.PV_NOT_BUFFERED,
        ErrorCode.FORMULA_EVAL_TIMEOUT,
        ErrorCode.ARCHIVER_UNAVAILABLE,
        ErrorCode.ARCHIVER_TIMEOUT,
        ErrorCode.UNHANDLED_EXCEPTION,
    }
)


def is_retryable(code: str) -> bool:
    """Value of ``retryable`` on an error frame fanned out by a live eval."""
    return code in TRANSIENT_ERROR_CODES
