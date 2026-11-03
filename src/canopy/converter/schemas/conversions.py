from typing import Any, Literal

from canopy.contracts.base import CanopyModel


class UnknownWidgetDTO(CanopyModel):
    """A legacy class with no registry mapping, rendered as a placeholder (D11)."""

    original_class: str
    count: int


class FormulaSummaryDTO(CanopyModel):
    """A hoisted ``calc://`` expression, lowered to a screen-level formula."""

    name: str
    expression: str


class ConversionStatsDTO(CanopyModel):
    widget_count: int
    macro_count: int
    rule_count: int


class ScreenConversionResultDTO(CanopyModel):
    """Result of converting one legacy screen to Canopy Screen IR.

    ``screen`` is the ``*.screen.json`` document the Screen Builder loads into
    the editor; the rest are import-time diagnostics.
    """

    screen: dict[str, Any]
    warnings: list[str] = []
    unknown_widgets: list[UnknownWidgetDTO] = []
    formulas: list[FormulaSummaryDTO] = []
    stats: ConversionStatsDTO


class ValidateResultDTO(CanopyModel):
    """Diagnostics-only result of a dry-run conversion (the IR is discarded)."""

    ok: bool
    warnings: list[str] = []
    unknown_widgets: list[UnknownWidgetDTO] = []


BatchRowStatus = Literal["succeeded", "failed", "skipped"]


class BatchManifestRowDTO(CanopyModel):
    """One archive entry's outcome in a batch conversion.

    ``result_key`` is the Redis key holding the converted screen IR; it is set
    only for ``succeeded`` rows and fetched via
    ``GET /conversions/batch/{jobId}/screens/{index}``.
    """

    path: str
    status: BatchRowStatus
    index: int | None = None
    warnings: list[str] = []
    unknown_widgets: list[UnknownWidgetDTO] = []
    result_key: str | None = None


class BatchScreenDTO(CanopyModel):
    """One converted screen's IR, fetched from a finished batch by index."""

    screen: dict[str, Any]


class BatchManifestDTO(CanopyModel):
    """Lightweight summary of a batch conversion — the Arq job result.

    The converted IR itself is *not* inlined here (a folder can hold thousands of
    screens); each ``succeeded`` row points at a per-screen Redis key the client
    fetches on demand. Read from ``JobStatusDTO.result`` after polling
    ``/jobs/v1/{jobId}``.
    """

    total: int
    succeeded: int
    failed: int
    skipped: int
    rows: list[BatchManifestRowDTO] = []
