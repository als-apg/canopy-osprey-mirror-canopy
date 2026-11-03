"""Formula Engine domain exceptions.

Services raise these. The per-module exception handler (registered via
``register_canopy_error_handler``) maps them to ``{"code": ..., "detail": ...}``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class FormulaEngineError(CanopyError):
    """Base for all Formula Engine errors."""


class FormulaParseError(FormulaEngineError):
    status_code = 400
    error_code = ErrorCode.FORMULA_PARSE_ERROR


class FormulaUnsafeError(FormulaEngineError):
    status_code = 400
    error_code = ErrorCode.FORMULA_UNSAFE


class FormulaInputMissing(FormulaEngineError):
    status_code = 422
    error_code = ErrorCode.FORMULA_INPUT_MISSING


class FormulaTypeError(FormulaEngineError):
    status_code = 422
    error_code = ErrorCode.FORMULA_TYPE_ERROR


class FormulaDimensionMismatch(FormulaEngineError):
    status_code = 422
    error_code = ErrorCode.FORMULA_DIMENSION_MISMATCH


class FormulaEvalTimeout(FormulaEngineError):
    status_code = 504
    error_code = ErrorCode.FORMULA_EVAL_TIMEOUT


class FormulaCircularReference(FormulaEngineError):
    status_code = 400
    error_code = ErrorCode.FORMULA_CIRCULAR_REFERENCE


class SubscriptionLimitExceeded(FormulaEngineError):
    status_code = 429
    error_code = ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED
