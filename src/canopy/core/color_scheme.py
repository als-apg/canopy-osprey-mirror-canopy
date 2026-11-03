"""The color scheme served to the viewer and the converter, flattened once per scheme file.

``get_color_scheme(path)`` flattens a ``canopy-color-scheme/1`` file onto the packaged ``canopy-default`` with
``pydmconverter.scheme.load_scheme`` and caches the result per resolved absolute path, so each file is read,
flattened and (when broken) logged once per process. Which file that is, is decided in one place,
``resolve_scheme_path``: an explicit path wins, else ``CANOPY_THEME_FILE``, else ``canopy-default`` alone.

Any error is served as ``canopy-default`` with the errors in ``meta`` and ``degraded == ["color-scheme"]``. When
``pydmconverter.scheme`` cannot be imported at all there is nothing to flatten with: ``result`` and ``flat_json``
are ``None`` and consumers fall back to their bundled default.

``canopy/core/data/canopy-default.json`` is the source of truth for the default scheme: pydmconverter vendors a
byte copy (``pydmconverter/ir/data/schemes/canopy-default.json``) and canopy-sdk-js ships a flattened copy. It
lives in the shared kernel so every process that serves a scheme can read it without the converter module.
"""

from __future__ import annotations

import os
import logging
import importlib
import threading
from typing import Any
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The canonical ``canopy-default`` scheme (pydmconverter's copy must stay byte-identical to it).
DEFAULT_SCHEME_PATH = Path(__file__).resolve().parent / "data" / "canopy-default.json"

DEFAULT_ID = "canopy-default"
THEME_FILE_ENV = "CANOPY_THEME_FILE"
DEGRADED_KEY = "color-scheme"


@dataclass(frozen=True)
class ColorScheme:
    """One flattened scheme as served.

    ``result`` is the ``SchemeResult`` for ``convert_bytes(scheme=...)`` (``None`` when the flattener is
    unavailable); ``flat_json`` is the flat scheme plus ``meta`` (``None`` likewise).
    """

    result: Any
    flat_json: dict[str, Any] | None
    meta: dict[str, Any]
    imports: dict[str, dict[str, str]]
    degraded: list[str]


_cache: dict[str, ColorScheme] = {}
_lock = threading.Lock()


def clear_color_scheme_cache() -> None:
    """Forget every cached scheme (tests, or a deliberate reload)."""
    with _lock:
        _cache.clear()


def resolve_scheme_path(path: str | os.PathLike[str] | None = None) -> Path | None:
    """The scheme file to serve: ``path`` when given, else ``CANOPY_THEME_FILE``.

    Returns the resolved absolute path, or ``None`` (serve ``canopy-default`` alone) when neither names a
    file; a blank ``CANOPY_THEME_FILE`` counts as unset. A module with a scheme setting of its own (the
    converter's ``scheme_path``) passes it here, so the fallback to the deployment-wide file is the same
    everywhere.
    """
    if path is None:
        env = os.environ.get(THEME_FILE_ENV, "").strip()
        if not env:
            return None
        path = env
    return Path(path).expanduser().resolve()


def get_color_scheme(path: str | os.PathLike[str] | None = None) -> ColorScheme:
    """The flattened scheme for ``path`` (see the module docstring), computed once per resolved path."""
    requested = resolve_scheme_path(path)
    key = str(requested) if requested is not None else f"<{DEFAULT_ID}>"
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            cached = _load(requested)
            _cache[key] = cached
    return cached


def _load(requested: Path | None) -> ColorScheme:
    label = str(requested) if requested is not None else DEFAULT_ID
    try:
        pydm_scheme = importlib.import_module("pydmconverter.scheme")
    except ImportError as exc:
        message = f"pydmconverter.scheme is unavailable: {exc}"
        logger.error("color scheme %s not loaded: %s; serving the bundled default", label, message)
        meta = {
            "id": None,
            "sha256": None,
            "flatHash": None,
            "served": DEFAULT_ID,
            "errors": [{"path": "", "message": message}],
        }
        return ColorScheme(result=None, flat_json=None, meta=meta, imports={}, degraded=[DEGRADED_KEY])

    result = pydm_scheme.load_scheme(requested, base_path=DEFAULT_SCHEME_PATH)
    errors = result.meta.get("errors") or []
    if errors:
        first = errors[0]
        logger.error(
            "color scheme %s has %d error(s), serving %s; first: %s: %s",
            label,
            len(errors),
            result.meta.get("served"),
            first.get("path") or "<root>",
            first.get("message"),
        )
    flat_json = result.flat_json()
    return ColorScheme(
        result=result,
        flat_json=flat_json,
        meta=flat_json["meta"],
        imports=result.imports,
        degraded=[DEGRADED_KEY] if errors else [],
    )
