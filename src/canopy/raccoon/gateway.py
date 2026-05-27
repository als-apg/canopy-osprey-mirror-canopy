"""Public surface of the Raccoon module for in-process callers.

Other Canopy modules import only from this file; the rest of `canopy.raccoon.*`
is private implementation. The signatures here form the contract that survives
internal refactoring.

This file is intentionally minimal in early commits. The async facades over
`DirectoryService` (list_pvs, exists, validate, ...) land in the slice that
introduces the service.
"""

from typing import Any

_service: Any = None


def bind(service: Any) -> None:
    """Wire the DirectoryService instance to the gateway. Called from `app.lifespan`."""
    global _service
    _service = service


def get_service() -> Any:
    if _service is None:
        raise RuntimeError("Raccoon gateway accessed before app startup bound a service")
    return _service
