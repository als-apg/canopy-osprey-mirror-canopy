import logging
from enum import StrEnum, auto

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from canopy.core.config import get_settings

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """Machine-readable error slugs returned in the `code` field of every error response.

    Slugs are auto-derived as lowercase snake_case from the member name. Add new
    slugs alongside the module that produces them.
    """

    UNHANDLED_EXCEPTION = auto()
    NO_CODE_GIVEN = auto()
    BAD_REQUEST = auto()
    NOT_FOUND = auto()

    INVALID_API_KEY = auto()
    INSUFFICIENT_READ_ACCESS = auto()
    INSUFFICIENT_WRITE_ACCESS = auto()

    # Mole
    ARCHIVER_NOT_CONFIGURED = auto()
    ARCHIVER_UNAVAILABLE = auto()
    ARCHIVER_TIMEOUT = auto()
    ARCHIVER_BAD_RESPONSE = auto()
    PV_NOT_FOUND_IN_ARCHIVER = auto()
    BINNING_NOT_SUPPORTED = auto()
    LAST_BEFORE_NOT_SUPPORTED = auto()

    # EPICS connector / Hare
    PV_CONNECTION_FAILED = auto()
    PV_READ_TIMEOUT = auto()
    PV_WRITE_FAILED = auto()
    EPICS_BACKEND_UNAVAILABLE = auto()


class CanopyError(Exception):
    """Base for all Canopy domain exceptions.

    Services raise these. The per-module exception handler registered via
    `register_canopy_error_handler` maps them to the uniform JSON response:

        {"code": "<slug>", "detail": "<msg>"}

    Services never import FastAPI / never raise HTTPException directly — that
    is what makes them extractable later without rewriting error sites.
    """

    status_code: int = 500
    error_code: ErrorCode = ErrorCode.UNHANDLED_EXCEPTION

    def __init__(self, detail: str = "") -> None:
        self.detail = detail or self.__class__.__name__
        super().__init__(self.detail)


class BadRequest(CanopyError):
    status_code = 400
    error_code = ErrorCode.BAD_REQUEST


class NotFound(CanopyError):
    status_code = 404
    error_code = ErrorCode.NOT_FOUND


class InvalidAPIKey(CanopyError):
    status_code = 401
    error_code = ErrorCode.INVALID_API_KEY


class InsufficientReadAccess(CanopyError):
    status_code = 403
    error_code = ErrorCode.INSUFFICIENT_READ_ACCESS


class InsufficientWriteAccess(CanopyError):
    status_code = 403
    error_code = ErrorCode.INSUFFICIENT_WRITE_ACCESS


def register_canopy_error_handler(app: FastAPI) -> None:
    """Register the Canopy `{code, detail}` JSON exception handlers on `app`.

    Each module's `app.py` calls this on its own sub-app. FastAPI's exception
    handlers do not propagate across `app.mount()` boundaries, so each mounted
    sub-app must register its own.
    """

    @app.exception_handler(CanopyError)
    async def canopy_error_handler(request: Request, exc: CanopyError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.error_code.value, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def fallback_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        debug = get_settings().debug
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.UNHANDLED_EXCEPTION.value,
                "detail": str(exc) if debug else "Internal server error",
            },
        )
