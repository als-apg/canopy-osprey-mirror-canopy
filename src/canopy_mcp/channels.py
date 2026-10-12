"""The renderer's channel walk: which strings in a node's props it subscribes to.

A port of the rule in the runtime's ``collectPVs`` (``canopy-screen-runtime``
``packages/runtime/src/render/collectPVs.ts``): every string under a prop key
that full-matches :data:`CHANNEL_KEY`, at any depth, except under
:data:`SKIPPED_PROPS`; nothing for a node the renderer does not mount as a
widget (an ``embedded-display``, an ``unknown-widget`` or a type outside
:data:`MAPPED_TYPES`). The screen walk, the semantic check and the lint's PV
pass read it from here, so they agree on what a node subscribes to.

Stdlib only; imports nothing from ``canopy_mcp``.
"""

from __future__ import annotations

import re
from typing import Any
from collections.abc import Iterator

Path = tuple[str | int, ...]

#: The IR types the renderer maps onto a widget (its ``TYPE_MAP``), sorted;
#: ``absolute-canvas`` and ``embedded-display`` are drawn by the renderer itself
#: and not listed. A test asserts this equals the vectors' ``mappedTypes``.
MAPPED_TYPES: tuple[str, ...] = (
    "arc",
    "ellipse",
    "frame",
    "group",
    "group-box",
    "image-view",
    "line",
    "polygon",
    "pv-array-plot",
    "pv-button",
    "pv-byte-led",
    "pv-checkbox",
    "pv-enum-combobox",
    "pv-gauge",
    "pv-label",
    "pv-led",
    "pv-meter",
    "pv-progress-bar",
    "pv-radio-group",
    "pv-slider",
    "pv-spinbox",
    "pv-tabs",
    "pv-tank",
    "pv-text-input",
    "pv-thermometer",
    "pv-toggle",
    "qwidget-container",
    "rectangle",
    "regular-button",
    "related-display-button",
    "scroll-area",
    "shell-command-button",
    "tabs",
    "text-label",
    "waveform-plot",
    "xy-plot",
)
_MAPPED = frozenset(MAPPED_TYPES)

EMBED_TYPE = "embedded-display"
UNKNOWN_TYPE = "unknown-widget"
_RENDERER_NATIVE_TYPES = frozenset({"absolute-canvas", EMBED_TYPE})

# Prop keys that name a PV channel, at any depth (JS `^(...)$`, so fullmatch).
CHANNEL_KEY = re.compile(r"pv|pvName|channel|[A-Za-z0-9]+_channels?|[a-z][A-Za-z0-9]*(?:Channel|PV)")
# Props never read for channels.
SKIPPED_PROPS = frozenset({"actions", "macros", "rules"})


def is_unmapped(ntype: Any) -> bool:
    """Whether the renderer draws ``ntype`` as no widget (an ``unknown-widget`` or a type it does not map)."""
    if not isinstance(ntype, str):
        return True
    return ntype == UNKNOWN_TYPE or (ntype not in _MAPPED and ntype not in _RENDERER_NATIVE_TYPES)


def mounts_children(ntype: Any) -> bool:
    """Whether the renderer mounts the children of a node of type ``ntype``."""
    return ntype != EMBED_TYPE and not is_unmapped(ntype)


def iter_channels(ntype: Any, props: Any) -> Iterator[tuple[Path, str]]:
    """``(path under props, string)`` for every channel a node's props name, in document order.

    A list adds its index to the path and keeps its key. Nothing for an embed,
    an unmapped type or ``props`` that is not a dict.
    """
    if ntype == EMBED_TYPE or is_unmapped(ntype) or not isinstance(props, dict):
        return
    for key, value in props.items():
        if key not in SKIPPED_PROPS:
            yield from _walk(value, key, (key,))


def _walk(value: Any, key: str, path: Path) -> Iterator[tuple[Path, str]]:
    if isinstance(value, str):
        if CHANNEL_KEY.fullmatch(key):
            yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk(item, key, (*path, i))
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in SKIPPED_PROPS:
                yield from _walk(v, k, (*path, k))
