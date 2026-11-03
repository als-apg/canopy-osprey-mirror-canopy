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
    INSUFFICIENT_ADMIN_ACCESS = auto()
    INSUFFICIENT_MODULE_ACCESS = auto()
    INSUFFICIENT_FACILITY_ACCESS = auto()
    API_KEY_NOT_FOUND = auto()
    API_KEY_SELF_DEACTIVATION = auto()
    KEY_STORE_UNAVAILABLE = auto()

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
    INVALID_PV_NAME = auto()

    # Raccoon
    DIRECTORY_UNAVAILABLE = auto()
    DIRECTORY_TIMEOUT = auto()
    INVALID_QUERY = auto()
    ADAPTER_MISCONFIGURED = auto()

    # Fox
    FORMULA_PARSE_ERROR = auto()
    FORMULA_UNSAFE = auto()
    FORMULA_INPUT_MISSING = auto()
    FORMULA_TYPE_ERROR = auto()
    FORMULA_DIMENSION_MISMATCH = auto()
    FORMULA_EVAL_TIMEOUT = auto()
    FORMULA_CIRCULAR_REFERENCE = auto()
    INVALID_MESSAGE = auto()
    INVALID_SPEC = auto()
    SUBSCRIPTION_LIMIT_EXCEEDED = auto()

    # Vole
    PV_NOT_BUFFERED = auto()
    BUFFER_AT_CAPACITY = auto()
    WINDOW_TOO_LARGE = auto()
    INGEST_UNAVAILABLE = auto()

    # Jobs
    JOB_NOT_FOUND = auto()
    JOB_ALREADY_EXISTS = auto()

    # Squirrel
    SNAPSHOT_NOT_FOUND = auto()
    PV_NOT_FOUND = auto()
    PV_ALREADY_EXISTS = auto()
    TAG_NOT_FOUND = auto()
    TAG_ALREADY_EXISTS = auto()
    TAG_GROUP_NOT_FOUND = auto()
    TAG_GROUP_ALREADY_EXISTS = auto()
    META_PV_NOT_FOUND = auto()
    FACILITY_NOT_FOUND = auto()
    FACILITY_SLUG_CONFLICT = auto()
    FACILITY_HAS_CHILDREN = auto()
    FACILITY_IN_USE = auto()
    FACILITY_CYCLE = auto()

    # Converter
    CONVERTER_UNSUPPORTED_INPUT = auto()
    CONVERTER_CONVERSION_FAILED = auto()
    CONVERTER_UPLOAD_TOO_LARGE = auto()
    CONVERTER_BATCH_RESULT_NOT_FOUND = auto()

    # Magpie
    CONFIG_NOT_FOUND = auto()
    CONFIG_SLUG_CONFLICT = auto()
    INVALID_APP_NAME = auto()

    # Analytics
    ANALYTICS_EVENT_TOO_LARGE = auto()


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


class InsufficientAdminAccess(CanopyError):
    status_code = 403
    error_code = ErrorCode.INSUFFICIENT_ADMIN_ACCESS


class InsufficientModuleAccess(CanopyError):
    """The key resolved, but is not scoped to the module this route belongs to.

    Orthogonal to read/write/admin — a key can have every scope and still be
    refused here if its ``modules`` set doesn't include this mount.
    """

    status_code = 403
    error_code = ErrorCode.INSUFFICIENT_MODULE_ACCESS


class InsufficientFacilityAccess(CanopyError):
    """The key resolved and has write access, but is not granted the facility the
    row being written belongs to.

    Orthogonal to read/write/admin and to module scope, like
    ``InsufficientModuleAccess`` — but raised from a module's service layer
    rather than during authentication, because the target facility is a property
    of the row, not of the request envelope.

    Reads have no equivalent refusal: an out-of-scope row is simply filtered out.
    """

    status_code = 403
    error_code = ErrorCode.INSUFFICIENT_FACILITY_ACCESS


class ApiKeyNotFound(CanopyError):
    status_code = 404
    error_code = ErrorCode.API_KEY_NOT_FOUND


class ApiKeySelfDeactivation(CanopyError):
    """Refused an admin key's request to deactivate itself over HTTP.

    Locking yourself out is recoverable via the CLI (it talks to the database
    directly), but that recovery path is out-of-band from a running deployment —
    worth a deliberate ``force=true`` rather than a request that happened to name
    the wrong id.
    """

    status_code = 400
    error_code = ErrorCode.API_KEY_SELF_DEACTIVATION


class KeyStoreUnavailable(CanopyError):
    """The key store could not be reached and no cached entry could answer.

    Deliberately distinct from `InvalidAPIKey`: 503 tells an operator the backend
    is down, where a 401 would send them hunting for a bad token.
    """

    status_code = 503
    error_code = ErrorCode.KEY_STORE_UNAVAILABLE


class FacilityNotFound(CanopyError):
    status_code = 404
    error_code = ErrorCode.FACILITY_NOT_FOUND


class FacilitySlugConflict(CanopyError):
    status_code = 409
    error_code = ErrorCode.FACILITY_SLUG_CONFLICT


class FacilityHasChildren(CanopyError):
    """Refused a delete/reparent — a facility with children must be emptied first."""

    status_code = 400
    error_code = ErrorCode.FACILITY_HAS_CHILDREN


class FacilityInUse(CanopyError):
    """Refused a delete — PVs or snapshots still reference this facility."""

    status_code = 400
    error_code = ErrorCode.FACILITY_IN_USE


class FacilityCycle(CanopyError):
    """Refused a reparent that would make a facility its own ancestor — includes
    the self-parent case, not just a deeper cycle."""

    status_code = 400
    error_code = ErrorCode.FACILITY_CYCLE


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
