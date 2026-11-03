from typing import Literal

from canopy.contracts.base import CanopyModel


class ElogAppHealthDTO(CanopyModel):
    """One app's token, probed with the provider's own identity lookup."""

    app: str
    healthy: bool
    # Who the e-log backend says the token is (elog-plus: the app's email).
    identity: str | None = None
    latency_ms: float | None = None
    last_error: str | None = None


class ElogHealthSummaryDTO(CanopyModel):
    """Rollup across configured apps.

    ``disabled`` when no usable provider is configured; ``down`` when a
    provider is set but no app has a token, or no app's probe succeeds; ``ok``
    when every app's probe succeeds, and ``degraded`` in between. ``reason``
    explains ``disabled`` and the no-token ``down``; it is ``None`` otherwise
    (per-app failures are in ``apps[].last_error``).
    """

    status: Literal["ok", "degraded", "down", "disabled"]
    provider: str | None = None
    reason: str | None = None
    apps: list[ElogAppHealthDTO]
