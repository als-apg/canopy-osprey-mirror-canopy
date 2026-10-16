"""The bundled EDM colors.list is valid, and conversions resolve palette indices through it."""

from pydmconverter.edm.parser_helpers import parse_colors_list

from canopy.converter.config import ConverterConfig
from canopy.converter.adapters.edm_lists import resolve_edm_lists
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


def test_resolve_edm_lists_returns_the_bundled_colors_list():
    """The default config resolves the bundled ``colors.list``."""
    config = ConverterConfig()
    colors, _ = resolve_edm_lists(config)
    assert colors == config.colors_list_path


def test_resolve_edm_lists_drops_a_missing_colors_list(tmp_path):
    """A configured ``colors.list`` with no file behind it resolves to ``None``."""
    colors, _ = resolve_edm_lists(ConverterConfig(colors_list_path=tmp_path / "absent.list"))
    assert colors is None


def test_conversion_resolves_indexed_colors_from_the_explicit_palette(monkeypatch):
    """A screen using ``fgColor index N`` carries the colour the bundled palette assigns that index.

    The palette reaches the converter only as the explicit path; the parser's
    own environment search is emptied.
    """
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    monkeypatch.delenv("EDMFILES", raising=False)
    colors, _ = resolve_edm_lists(ConverterConfig())

    result = ConversionService(colors_list_path=colors).convert(_INDEXED_COLOR_EDL, kind="edl")

    assert result.stats.widget_count == 1
    label = result.screen["root"]["children"][0]
    assert label["type"] == "text-label"
    assert label["props"]["text"] == "Indexed colours"
    assert parse_colors_list(str(colors))["static"][25] == {"name": "Controller", "rgb": [0, 0, 65535]}
    assert label["props"]["foregroundColor"] == "#0000ff"


def test_conversion_resolves_the_screen_background_from_the_explicit_palette(monkeypatch):
    """A screen declaring ``bgColor index N`` carries the palette's colour on its root canvas.

    The parser's own environment search is emptied, so the colour can only come
    from the explicit palette.
    """
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    monkeypatch.delenv("EDMFILES", raising=False)
    colors, _ = resolve_edm_lists(ConverterConfig())
    edl = _INDEXED_COLOR_EDL.replace(b"endScreenProperties", b"bgColor index 25\nendScreenProperties", 1)

    result = ConversionService(colors_list_path=colors).convert(edl, kind="edl")

    assert result.screen["root"]["props"]["backgroundColor"] == "#0000ff"
