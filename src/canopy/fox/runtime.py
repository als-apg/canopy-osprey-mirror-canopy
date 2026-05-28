"""Module-level runtime state for Fox.

Kept separate from ``app.py`` so the API layer can read uptime without an import
cycle through the FastAPI app.
"""

import time

_start_monotonic: float = time.monotonic()


def mark_started() -> None:
    """Record the boot time. Called from the parent app's lifespan startup."""
    global _start_monotonic
    _start_monotonic = time.monotonic()


def uptime_s() -> float:
    return time.monotonic() - _start_monotonic
