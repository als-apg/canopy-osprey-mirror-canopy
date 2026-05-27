"""Raccoon domain error classes carry the right HTTP status and code slug."""

from canopy.core.errors import ErrorCode
from canopy.raccoon.adapters.base import (
    InvalidQuery,
    DirectoryTimeout,
    AdapterMisconfigured,
    DirectoryUnavailable,
)


def test_directory_unavailable_envelope() -> None:
    err = DirectoryUnavailable("upstream down")
    assert err.status_code == 503
    assert err.error_code is ErrorCode.DIRECTORY_UNAVAILABLE
    assert err.detail == "upstream down"


def test_directory_timeout_inherits_unavailable_with_504() -> None:
    err = DirectoryTimeout("slow")
    assert isinstance(err, DirectoryUnavailable)
    assert err.status_code == 504
    assert err.error_code is ErrorCode.DIRECTORY_TIMEOUT


def test_invalid_query_envelope() -> None:
    err = InvalidQuery("bad regex")
    assert err.status_code == 400
    assert err.error_code is ErrorCode.INVALID_QUERY


def test_adapter_misconfigured_envelope() -> None:
    err = AdapterMisconfigured("missing url")
    assert err.status_code == 500
    assert err.error_code is ErrorCode.ADAPTER_MISCONFIGURED
