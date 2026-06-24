from canopy.core.errors import ErrorCode, CanopyError


class JobsError(CanopyError):
    """Base for Jobs-module domain errors."""


class JobNotFound(JobsError):
    status_code = 404
    error_code = ErrorCode.JOB_NOT_FOUND


class JobAlreadyExists(JobsError):
    status_code = 409
    error_code = ErrorCode.JOB_ALREADY_EXISTS
