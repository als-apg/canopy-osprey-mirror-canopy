from typing import Any

from canopy.contracts.base import CanopyModel


class UnknownWidgetDTO(CanopyModel):
    """A legacy class with no registry mapping, rendered as a placeholder (D11)."""

    original_class: str
    count: int


class FormulaSummaryDTO(CanopyModel):
    """A hoisted ``calc://`` expression, lowered to a screen-level Fox formula."""

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
