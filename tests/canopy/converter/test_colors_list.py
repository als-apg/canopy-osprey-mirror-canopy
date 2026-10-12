"""The bundled EDM colors.list is valid, and the service hands it to the converter."""

import os

from pydmconverter.edm.parser_helpers import parse_colors_list

from canopy.converter.config import ConverterConfig
from canopy.converter.services.conversion_service import ConversionService

# Minimal EDM screen whose only widget declares palette-indexed colours
# (``fgColor index N``), the form the display corpus uses universally --
# unlike the ``rgb r g b`` literals in the p0/p1 fixtures.
_INDEXED_COLOR_EDL = b"""4 0 0
beginScreenProperties
major 4
minor 0
release 0
x 0
y 0
w 200
h 100
font "helvetica-medium-r-12.0"
endScreenProperties

# (Static Text)
object activeXTextClass
beginObjectProperties
major 4
minor 1
release 1
x 10
y 20
w 120
h 18
font "helvetica-medium-r-12.0"
fgColor index 25
bgColor index 3
value {
  "Indexed colours"
}
endObjectProperties
"""


def test_bundled_colors_list_exists_and_parses_cleanly():
    path = ConverterConfig().colors_list_path
    assert path.is_file(), f"bundled colors.list missing at {path}"
    data = parse_colors_list(str(path))
    assert data["version"] == {"major": 4, "minor": 0, "release": 0}
    # Base palette: verbatim from the SLAC display corpus (bcs/config/colors.list).
    assert data["static"][0] == {"name": "Disconn/Invalid", "rgb": [65535, 65535, 65535]}
    assert data["static"][14] == {"name": "black-14", "rgb": [0, 0, 0]}
    # Blinking colours round-trip with all six components.
    assert len(data["static"][83]["rgb"]) == 6
    # Synthetic extension: every residual index the corpus references resolves.
    assert data["static"][193]["name"] == "site-ext-193"
    assert len(data["static"]) == 101


def test_the_service_hands_the_palette_to_the_converter(monkeypatch):
    """A screen using ``fgColor index N`` resolves through the configured palette, whatever the environment says."""
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    monkeypatch.delenv("EDMFILES", raising=False)
    config = ConverterConfig()
    result = ConversionService(color_list_path=config.colors_list_path).convert(_INDEXED_COLOR_EDL, kind="edl")
    assert result.stats.widget_count == 1
    label = result.screen["root"]["children"][0]
    assert label["type"] == "text-label"
    assert label["props"]["text"] == "Indexed colours"
    # Index 25 is "Controller", rgb (0, 0, 65535) in the bundled palette.
    assert label["props"]["foregroundColor"] == "#0000ff"


def test_a_missing_palette_degrades_to_the_converter_default(tmp_path, monkeypatch):
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    monkeypatch.delenv("EDMFILES", raising=False)
    result = ConversionService(color_list_path=tmp_path / "absent.list").convert(_INDEXED_COLOR_EDL, kind="edl")
    assert result.stats.widget_count == 1
    assert result.screen["root"]["children"][0]["props"]["text"] == "Indexed colours"


def test_the_environment_is_not_written(monkeypatch):
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    ConversionService(color_list_path=ConverterConfig().colors_list_path).convert(_INDEXED_COLOR_EDL, kind="edl")
    assert "EDMCOLORFILE" not in os.environ
