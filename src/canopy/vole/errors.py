from canopy.core.errors import ErrorCode, CanopyError


class VoleError(CanopyError):
    """Base for all Vole buffer errors."""


class PVNotBuffered(VoleError):
    status_code = 404
    error_code = ErrorCode.PV_NOT_BUFFERED


class BufferAtCapacity(VoleError):
    status_code = 503
    error_code = ErrorCode.BUFFER_AT_CAPACITY
