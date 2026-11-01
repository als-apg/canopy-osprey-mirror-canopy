"""Stateless conversion of legacy screen bytes into Canopy Screen IR.

Pure domain logic — no FastAPI, no persistence. Wraps the ``pydmconverter``
pipeline (``convert_bytes`` -> ``ScreenIR``), validates the emitted IR against
the Screen IR schema, and summarizes convert-time diagnostics (warnings,
unknown widgets, hoisted formulas) for the API layer.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, get_args
from pathlib import Path
from dataclasses import field, replace, dataclass
from collections.abc import Iterator, Sequence

from pydmconverter.react import convert_bytes
from pydmconverter.ir.emit import to_wire_dict
from pydmconverter.ir.model import ScreenIR, WidgetNode
from pydmconverter.ir.schema import validate_screen_json
from pydmconverter.ir.registry import VendoredRegistry

from canopy.converter.errors import ConversionFailed, UnsupportedInput
from canopy.core.color_scheme import ColorScheme

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
class DroppedProps:
    """Legacy properties of one converted node that the IR could not carry."""

    node_id: str
    props: list[str]


@dataclass(frozen=True)
class ConversionResult:
    """Domain result of a single screen conversion.

    ``screen`` is the validated camelCase Screen IR wire dict; the rest are
    diagnostics rolled up from its nodes (screen-level notes ride on the root canvas).
    ``scheme`` is the meta of the color scheme the screen was converted with
    (``None`` for a scheme-free service).
    """

    screen: dict[str, Any]
    warnings: list[str]
    unknown_widgets: list[UnknownWidget]
    formulas: list[ConvertedFormula]
    stats: ConversionStats
    dropped_props: list[DroppedProps] = field(default_factory=list)
    scheme: dict[str, Any] | None = None


ScreenKind = Literal["edl", "ui", "bob"]
"""Legacy screen formats the converter accepts."""

SCREEN_KINDS: tuple[ScreenKind, ...] = get_args(ScreenKind)

SUFFIX_KIND: dict[str, ScreenKind] = {".edl": "edl", ".ui": "ui", ".bob": "bob"}
"""Lower-case file suffix -> screen kind."""


class ConversionService:
    """Converts ``.edl``/``.ui``/``.bob`` bytes to validated Screen IR + diagnostics.

    Parameters
    ----------
    scheme : ColorScheme, optional
        The already-loaded ``get_color_scheme`` result named colors map onto;
        ``None`` keeps the service scheme-free.
    colors_list_path : Path, optional
        EDM ``colors.list`` used to resolve palette-indexed colours.
    calc_list_path : Path, optional
        EDM ``calc.list`` used to resolve named ``CALC\\`` PVs.
    color_def_path : Path, optional
        Phoebus ``color.def`` the converter resolves named colors from; omitted
        from ``convert_bytes`` when unset.
    search_paths : sequence of str or Path, optional
        ``EDMDATAFILES``-style directories searched for files an EDM screen
        references, such as symbol files. Used for ``.edl`` conversions only.
    """

    def __init__(
        self,
        *,
        scheme: ColorScheme | None = None,
        colors_list_path: str | Path | None = None,
        calc_list_path: str | Path | None = None,
        color_def_path: str | Path | None = None,
        search_paths: Sequence[str | Path] = (),
    ) -> None:
        # One registry instance, reused across requests (the adapters would
        # otherwise rebuild a VendoredRegistry per call).
        self._registry = VendoredRegistry()
        # ``scheme`` is the already-loaded ``get_color_scheme`` result, injected by
        # the app lifespan and the batch worker; the service never loads one itself.
        # ``None`` keeps the service scheme-free, converting exactly as before.
        self._scheme = scheme
        self._colors_list_path = colors_list_path
        self._calc_list_path = calc_list_path
        self._color_def_path = color_def_path
        # EDMDATAFILES-style directories for symbol files (EDM screens only).
        self._search_paths = [str(p) for p in search_paths]
        self._scheme_warning = _scheme_warning(scheme)

    @property
    def scheme_meta(self) -> dict[str, Any] | None:
        """The injected scheme's meta (with ``flatHash``), or ``None`` when scheme-free."""
        return self._scheme.meta if self._scheme is not None else None

    def _convert_kwargs(self, kind: ScreenKind, filename: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "color_list_path": self._colors_list_path,
            "calc_list_path": self._calc_list_path,
            "filename": filename,
            "search_paths": self._search_paths if kind == "edl" and self._search_paths else None,
            "scheme": self._scheme.result if self._scheme is not None else None,
        }
        if self._color_def_path is not None:
            kwargs["color_def_path"] = self._color_def_path
        return kwargs

    def convert(self, data: bytes, *, kind: ScreenKind, filename: str | None = None) -> ConversionResult:
        """Convert one screen. ``filename`` is the upload's original name; the screen
        id and title derive from it (without it both are ``"screen"``).

        Raises ``UnsupportedInput`` (415) for a kind outside ``SCREEN_KINDS`` and
        ``ConversionFailed`` (422) for any failure to convert a screen of a known kind.
        """
        # Reject an unsupported kind here rather than by catching convert_bytes'
        # ValueError: its parsers (e.g. the .bob XML reader) also raise ValueError for
        # malformed input on a *known* kind, which is a 422, not a 415.
        if kind not in SCREEN_KINDS:
            raise UnsupportedInput(f"Unsupported screen kind {kind!r}; expected one of {', '.join(SCREEN_KINDS)}")
        try:
            screen = convert_bytes(data, kind=kind, registry=self._registry, **self._convert_kwargs(kind, filename))
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

        result = self._summarize(screen, wire)
        warnings = result.warnings if self._scheme_warning is None else [self._scheme_warning, *result.warnings]
        return replace(result, warnings=warnings, scheme=self.scheme_meta)

    @staticmethod
    def _summarize(screen: ScreenIR, wire: dict[str, Any]) -> ConversionResult:
        warnings: list[str] = []
        unknown_counts: dict[str, int] = {}
        rule_count = 0
        node_count = 0
        dropped: list[DroppedProps] = []
        for node in _walk(screen.root):
            node_count += 1
            if node.dropped_props:
                dropped.append(DroppedProps(node_id=node.id, props=list(node.dropped_props)))
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
            dropped_props=dropped,
        )


def _scheme_warning(scheme: ColorScheme | None) -> str | None:
    """The one warning every conversion carries after a scheme load error, else ``None``."""
    if scheme is None:
        return None
    errors = scheme.meta.get("errors") or []
    if not errors:
        return None
    first = errors[0]
    return (
        f"Color scheme could not be loaded ({len(errors)} error(s)); converted with "
        f"{scheme.meta.get('served')}. First: {first.get('path') or '<root>'}: {first.get('message')}"
    )


def _walk(node: WidgetNode) -> Iterator[WidgetNode]:
    yield node
    for child in node.children:
        yield from _walk(child)
