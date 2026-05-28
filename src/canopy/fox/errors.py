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
