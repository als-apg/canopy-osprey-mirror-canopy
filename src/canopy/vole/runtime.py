import time

_start_monotonic: float = time.monotonic()


def mark_started() -> None:
    global _start_monotonic
    _start_monotonic = time.monotonic()


def uptime_s() -> float:
    return time.monotonic() - _start_monotonic
