"""Public surface of the Mole module for in-process callers.

Other Canopy modules import only from this file; the rest of `canopy.mole.*`
is private implementation. The signatures here form the contract that survives
adapter churn and the eventual extraction of Mole into its own service.

This file is intentionally minimal in early commits — service methods land in
Commit 5 alongside the routes they back.
"""

from typing import Any

_service: Any = None


def bind(service: Any) -> None:
    """Wire the ArchiverService instance to the gateway. Called from `app.lifespan`."""
    global _service
    _service = service


def _require_service() -> Any:
    if _service is None:
        raise RuntimeError("Mole gateway accessed before app startup bound a service")
    return _service
