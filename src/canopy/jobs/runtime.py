"""Process start-time tracking for the Jobs health endpoint.

Kept separate from app.py so health routes can read uptime without importing
the app module (avoids an import cycle).
"""

import time

_started_monotonic = time.monotonic()


def mark_started() -> None:
    global _started_monotonic
    _started_monotonic = time.monotonic()


def uptime_s() -> float:
    return time.monotonic() - _started_monotonic
