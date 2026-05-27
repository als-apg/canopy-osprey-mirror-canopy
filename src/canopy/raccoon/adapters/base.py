"""Adapter contract and domain exception hierarchy for Raccoon.

`DirectoryAdapter` is a Protocol; concrete adapters (the SLAC `MemeDirectoryAdapter`,
out-of-tree labs') implement it. `register_directory_type` registers a short
name; the `module:Class` dotted-path form in config is resolved at registry-build
time without needing prior registration.
"""

from typing import Protocol, runtime_checkable

from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.base import CanopyModel
from canopy.raccoon.config import DirectoryInstanceConfig

# --------------------------------------------------------------------------- #
# Domain exceptions. Services raise these; the per-module handler maps them
# to {"code": <slug>, "detail": ...} JSON.
# --------------------------------------------------------------------------- #


class RaccoonError(CanopyError):
    """Base for all Raccoon errors."""


class DirectoryUnavailable(RaccoonError):
    status_code = 503
    error_code = ErrorCode.DIRECTORY_UNAVAILABLE


class DirectoryTimeout(DirectoryUnavailable):
    status_code = 504
    error_code = ErrorCode.DIRECTORY_TIMEOUT


class InvalidQuery(RaccoonError):
    status_code = 400
    error_code = ErrorCode.INVALID_QUERY


class AdapterMisconfigured(RaccoonError):
    status_code = 500
    error_code = ErrorCode.ADAPTER_MISCONFIGURED


# --------------------------------------------------------------------------- #
# Adapter health DTO
# --------------------------------------------------------------------------- #


class AdapterHealth(CanopyModel):
    healthy: bool
    latency_ms: float | None = None
    last_error: str | None = None
    last_check_ts_ns: int


# --------------------------------------------------------------------------- #
# Adapter Protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class DirectoryAdapter(Protocol):
    """Contract every directory-service adapter must satisfy.

    See `Module-Architecture-Raccoon.md` §12 for the full specification. The
    `name` field is the type slug (or the dotted-path class name) injected by
    the registry; Raccoon configures exactly one adapter, so it doubles as the
    instance label in logs and stats.
    """

    name: str
    type_name: str

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "DirectoryAdapter":
        """Build an instance from validated config."""
        ...

    async def list_pvs(
        self,
        pattern: str,
        *,
        tag: str | None,
        sort_by: str | None,
        element_type: str | None,
    ) -> list[str]: ...

    async def list_devices(
        self,
        pattern: str,
        *,
        tag: str | None,
        sort_by: str | None,
        element_type: str | None,
    ) -> list[str]: ...

    async def list_elements(
        self,
        pattern: str,
        *,
        tag: str | None,
        sort_by: str | None,
        element_type: str | None,
    ) -> list[str]: ...

    async def health(self) -> AdapterHealth:
        """Cheap reachability check."""
        ...

    async def close(self) -> None:
        """Drain or release any underlying client. Called from app.lifespan shutdown."""
        ...


# --------------------------------------------------------------------------- #
# Type registration: short-name -> class. Out-of-tree adapters can skip this
# and use the `module:Class` dotted-path form in config instead.
# --------------------------------------------------------------------------- #

_REGISTERED_TYPES: dict[str, type] = {}


def register_directory_type(name: str):
    """Decorator that registers an adapter class under a short type name.

    Example::

        @register_directory_type("meme")
        class MemeDirectoryAdapter:
            ...

    Then ``cfg.type = "meme"`` resolves to this class.
    """

    def deco(cls: type) -> type:
        cls.type_name = name
        _REGISTERED_TYPES[name] = cls
        return cls

    return deco


def get_registered_type(name: str) -> type | None:
    return _REGISTERED_TYPES.get(name)
