"""Golden regression: a real .edl converts to an exact, schema-valid screen.json.

The committed ``widgets.screen.json`` is the expected output of the pinned
pydmconverter. If an intentional upstream change alters the IR, regenerate it by
writing ``ConversionService().convert(<edl bytes>, kind="edl").screen`` back to
the fixture; an *unintentional* diff here is the regression this test guards.
"""

import json
from pathlib import Path

from pydmconverter.ir.schema import validate_screen_json

from canopy.converter.services.conversion_service import ConversionService

FIXTURES = Path(__file__).parent / "fixtures"


def test_widgets_edl_matches_golden_screen_json():
    expected = json.loads((FIXTURES / "widgets.screen.json").read_text(encoding="utf-8"))
    result = ConversionService().convert((FIXTURES / "widgets.edl").read_bytes(), kind="edl")
    assert result.screen == expected
    assert validate_screen_json(result.screen) == []  # conforms to the Screen IR schema
