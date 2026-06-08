import time
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int: ...


class SystemClock:
    """System clock: UTC nanoseconds from time.time_ns()."""

    def now_ns(self) -> int:
        return time.time_ns()


class FakeClock:
    """Test clock: manually advanced."""

    def __init__(self, start_ns: int = 0) -> None:
        self._now = start_ns

    def now_ns(self) -> int:
        return self._now

    def advance(self, delta_ns: int) -> None:
        self._now += delta_ns

    def set(self, ns: int) -> None:
        self._now = ns
