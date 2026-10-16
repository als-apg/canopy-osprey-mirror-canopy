"""Stateless conversion of legacy screen bytes into Canopy Screen IR.

Pure domain logic — no FastAPI, no persistence. Wraps the ``pydmconverter``
pipeline (``convert_bytes`` -> ``ScreenIR``), validates the emitted IR against
the Screen IR schema, and summarizes convert-time diagnostics (warnings,
unknown widgets, hoisted formulas) for the API layer.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Iterator, Sequence

from pydmconverter.react import convert_bytes
from pydmconverter.ir.emit import to_wire_dict
from pydmconverter.ir.model import ScreenIR, WidgetNode
from pydmconverter.ir.schema import validate_screen_json
from pydmconverter.ir.registry import VendoredRegistry

from canopy.converter.errors import ConversionFailed, UnsupportedInput

logger = logging.getLogger(__name__)

# The placeholder node type the builder emits for any class it cannot resolve
# (pydmconverter.ir.builder.UNKNOWN_WIDGET_TYPE, D11). Kept in sync by test.
_UNKNOWN_WIDGET_TYPE = "unknown-widget"


@dataclass(frozen=True)
class UnknownWidget:
    original_class: str
    count: int


@dataclass(frozen=True)
class ConvertedFormula:
    """A hoisted ``calc://`` formula, shape-compatible with ``FormulaSpec``."""

    name: str
    kind: Literal["scalar", "timeseries", "waveform"]
    expression: str
    bindings: dict[str, str]


@dataclass(frozen=True)
class ConversionStats:
    widget_count: int
    macro_count: int
    rule_count: int


@dataclass(frozen=True)
class ConversionResult:
    """Domain result of a single screen conversion.

    ``screen`` is the validated camelCase Screen IR wire dict; the rest are
    diagnostics rolled up from its nodes (screen-level notes ride on the root canvas).
    """

    screen: dict[str, Any]
    warnings: list[str]
    unknown_widgets: list[UnknownWidget]
    formulas: list[ConvertedFormula]
    stats: ConversionStats


class ConversionService:
    """Converts ``.edl``/``.ui`` bytes to validated Screen IR + diagnostics.

    Parameters
    ----------
    colors_list_path : Path, optional
        EDM ``colors.list`` used to resolve palette-indexed colours.
    calc_list_path : Path, optional
        EDM ``calc.list`` used to resolve named ``CALC\\`` PVs.
    search_paths : sequence of str or Path, optional
        ``EDMDATAFILES``-style directories searched for files an EDM screen
        references, such as symbol files. Used for ``.edl`` conversions only.
    """

    def __init__(
        self,
        *,
        colors_list_path: Path | None = None,
        calc_list_path: Path | None = None,
        search_paths: Sequence[str | Path] = (),
    ) -> None:
        # One registry instance, reused across requests (the adapters would
        # otherwise rebuild a VendoredRegistry per call).
        self._registry = VendoredRegistry()
        self._colors_list_path = colors_list_path
        self._calc_list_path = calc_list_path
        # EDMDATAFILES-style directories for symbol files (EDM screens only).
        self._search_paths = [str(p) for p in search_paths]

    def convert(self, data: bytes, *, kind: Literal["edl", "ui"], filename: str | None = None) -> ConversionResult:
        """Convert one screen. ``filename`` is the upload's original name; the screen
        id and title derive from it (without it both are ``"screen"``)."""
        try:
            screen = convert_bytes(
                data,
                kind=kind,
                registry=self._registry,
                color_list_path=self._colors_list_path,
                calc_list_path=self._calc_list_path,
                filename=filename,
                search_paths=self._search_paths if kind == "edl" and self._search_paths else None,
            )
        except ValueError as exc:
            # convert_bytes raises ValueError for an unsupported kind/suffix.
            raise UnsupportedInput(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - any parse/build failure on a known kind
            # The catch stays broad on purpose: pydmconverter raises bare ValueError
            # *and* KeyError for legitimately malformed input, the same types a server
            # bug or API drift would raise, so the exception type cannot tell them
            # apart. Mapping all of it to 422 keeps malformed input from 500ing; we log
            # here so a genuine server-side bug is still observable in the traceback
            # rather than silently masked as "your input is invalid".
            logger.exception("convert_bytes failed for a %s screen", kind)
            raise ConversionFailed(f"Failed to convert {kind} screen: {exc}") from exc

        wire = to_wire_dict(screen)
        errors = validate_screen_json(wire)
        if errors:
            raise ConversionFailed("Emitted IR failed schema validation: " + "; ".join(errors[:5]))

        return self._summarize(screen, wire)

    @staticmethod
    def _summarize(screen: ScreenIR, wire: dict[str, Any]) -> ConversionResult:
        warnings: list[str] = []
        unknown_counts: dict[str, int] = {}
        rule_count = 0
        node_count = 0
        for node in _walk(screen.root):
            node_count += 1
            warnings.extend(node.warnings)
            rule_count += len(node.rules)
            if node.type == _UNKNOWN_WIDGET_TYPE:
                original = str(node.props.get("originalClass", "unknown"))
                unknown_counts[original] = unknown_counts.get(original, 0) + 1

        unknown = [UnknownWidget(original_class=name, count=count) for name, count in sorted(unknown_counts.items())]
        formulas = [
            ConvertedFormula(name=f.name, kind=f.kind, expression=f.expression, bindings=dict(f.bindings))
            for f in screen.formulas
        ]
        stats = ConversionStats(
            # Exclude the root canvas node; count the widgets placed on it.
            widget_count=max(node_count - 1, 0),
            macro_count=len(screen.macros),
            rule_count=rule_count,
        )
        return ConversionResult(
            screen=wire,
            warnings=warnings,
            unknown_widgets=unknown,
            formulas=formulas,
            stats=stats,
        )


def _walk(node: WidgetNode) -> Iterator[WidgetNode]:
    yield node
    for child in node.children:
        yield from _walk(child)
