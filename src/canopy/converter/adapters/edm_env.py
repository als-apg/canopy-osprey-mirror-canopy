"""Wire the EDM parser's environment to the bundled/configured ``colors.list``.

The reused EDM parser resolves colours via ``search_color_list()``:
``EDMCOLORFILE`` -> ``$EDMFILES/colors.list`` -> ``/etc/edm/colors.list``. We
point ``EDMCOLORFILE`` at the module's bundled (or config-overridden) file at
startup so conversions are deterministic and independent of the host's EDM
install. A missing file degrades gracefully (the parser returns empty colour
data), so this only improves fidelity — it never blocks conversion.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

from canopy.converter.config import ConverterConfig

logger = logging.getLogger(__name__)


def apply_edm_colors(config: ConverterConfig) -> Path | None:
    """Point ``EDMCOLORFILE`` at the configured ``colors.list`` when it exists.

    Returns the path applied, or ``None`` when the file is absent.
    """
    path = config.colors_list_path
    if path.is_file():
        os.environ["EDMCOLORFILE"] = str(path)
        logger.info("EDM colors.list -> %s", path)
        return path
    logger.warning("colors.list not found at %s; EDM bgColor resolution will fall back", path)
    return None
