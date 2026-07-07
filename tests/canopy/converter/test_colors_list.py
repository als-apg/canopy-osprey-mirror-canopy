"""The bundled EDM colors.list is valid, and the env wiring points the parser at it."""

import os

from pydmconverter.edm.parser_helpers import parse_colors_list, search_color_list

from canopy.converter.config import ConverterConfig
from canopy.converter.adapters.edm_env import apply_edm_colors
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


def test_apply_edm_colors_points_env_at_bundled_file(monkeypatch):
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    applied = apply_edm_colors(ConverterConfig())
    assert applied is not None
    assert os.environ["EDMCOLORFILE"] == str(applied)
    # The parser's own resolver now finds our file first.
    assert search_color_list() == str(applied)


def test_apply_edm_colors_missing_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    cfg = ConverterConfig(colors_list_path=tmp_path / "absent.list")
    assert apply_edm_colors(cfg) is None


def test_conversion_resolves_indexed_colors_via_env(monkeypatch):
    """A screen using ``fgColor index N`` converts with the env-provisioned palette.

    The pinned pydmconverter (1.0.4) resolves indices in its EDM parser but does
    not yet surface colours in the emitted IR, so this pins the plumbing: the
    conversion succeeds end-to-end and the parser-side resolver maps index 25
    through our EDMCOLORFILE wiring to the bundled palette's value.

    TODO(pydm-converter-tool#150 + dep bump): once the pinned pydmconverter
    grows ``color_list_path=`` and IR colour props, pass the path explicitly
    and tighten this to assert the emitted widget carries the resolved
    ``foregroundColor``.
    """
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    applied = apply_edm_colors(ConverterConfig())
    assert applied is not None

    result = ConversionService().convert(_INDEXED_COLOR_EDL, kind="edl")
    assert result.stats.widget_count == 1
    label = result.screen["root"]["children"][0]
    assert label["type"] == "text-label"
    assert label["props"]["text"] == "Indexed colours"

    # The parser's resolver, pointed at our file, resolves the index the screen used.
    assert parse_colors_list(search_color_list())["static"][25] == {"name": "Controller", "rgb": [0, 0, 65535]}
