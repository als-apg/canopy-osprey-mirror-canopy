from canopy.core.errors import ErrorCode, CanopyError


class VoleError(CanopyError):
    """Base for all Vole buffer errors."""


class PVNotBuffered(VoleError):
    status_code = 404
    error_code = ErrorCode.PV_NOT_BUFFERED

    def __init__(self, detail: str = "", *, pvs: list[str] | None = None) -> None:
        super().__init__(detail)
        self.pvs: list[str] = pvs or []


class BufferAtCapacity(VoleError):
    status_code = 503
    error_code = ErrorCode.BUFFER_AT_CAPACITY
