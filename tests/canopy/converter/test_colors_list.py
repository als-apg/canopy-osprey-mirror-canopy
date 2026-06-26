"""The bundled EDM colors.list is valid, and the env wiring points the parser at it."""

import os

from pydmconverter.edm.parser_helpers import parse_colors_list, search_color_list

from canopy.converter.config import ConverterConfig
from canopy.converter.adapters.edm_env import apply_edm_colors


def test_bundled_colors_list_exists_and_parses_cleanly():
    path = ConverterConfig().colors_list_path
    assert path.is_file(), f"bundled colors.list missing at {path}"
    data = parse_colors_list(str(path))
    assert data["version"] == {"major": 4, "minor": 0, "release": 0}
    assert data["static"][0]["rgb"] == [65535, 65535, 65535]  # grey-100 / white
    assert data["static"][14]["name"] == "red"


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
