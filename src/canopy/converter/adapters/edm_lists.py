"""Resolve the EDM ``colors.list`` and ``calc.list`` handed to the converter.

``convert_bytes`` takes both as explicit paths, each of which wins over the EDM
parser's own search of the environment (``EDMCOLORFILE``, ``$EDMFILES``,
``/etc/edm``). Pointing them at the bundled or configured files keeps
conversions deterministic and independent of the host's EDM install. A
configured file that does not exist is logged and left out, so the parser falls
back to its own search and conversion is never blocked: colours degrade to
unresolved indices and named ``CALC\\`` PVs stay untranslated, both surfacing
as convert-time warnings.
"""

from __future__ import annotations

import logging
from pathlib import Path

from canopy.converter.config import ConverterConfig

logger = logging.getLogger(__name__)


def resolve_edm_lists(config: ConverterConfig) -> tuple[Path | None, Path | None]:
    """Return the ``colors.list`` and ``calc.list`` paths to pass to the converter.

    ``calc_list_path`` takes the configured file when it exists. Otherwise a
    ``calc.list`` beside the resolved ``colors.list`` is used, since a site's
    EDM config directory usually holds both.

    Parameters
    ----------
    config : ConverterConfig
        Converter settings; ``colors_list_path`` and ``calc_list_path`` select
        the files.

    Returns
    -------
    tuple of (Path or None, Path or None)
        The ``colors.list`` and ``calc.list`` paths, each ``None`` when no
        existing file was found.
    """
    colors_list = config.colors_list_path
    if not colors_list.is_file():
        logger.warning("colors.list not found at %s; EDM colour resolution will fall back", colors_list)
        colors_list = None

    calc_list = config.calc_list_path
    if calc_list is not None and not calc_list.is_file():
        logger.warning("calc.list not found at %s; named CALC PVs will stay untranslated", calc_list)
        calc_list = None
    if calc_list is None and colors_list is not None and (colors_list.parent / "calc.list").is_file():
        calc_list = colors_list.parent / "calc.list"

    return colors_list, calc_list
