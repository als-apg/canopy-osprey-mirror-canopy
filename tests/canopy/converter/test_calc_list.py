"""Tests for the ``calc.list`` configuration knob.

``CANOPY_CONVERTER__CALC_LIST_PATH`` populates ``ConverterConfig.calc_list_path``;
startup resolves it with `resolve_edm_lists` and hands it to `ConversionService`,
which passes it to the EDM parser as its explicit calc list.
"""

import logging
from pathlib import Path

import pytest

import canopy.converter.app as converter_app
from canopy.converter import gateway
from canopy.converter.config import ConverterConfig
from canopy.converter.adapters import edm_lists
from canopy.converter.adapters.edm_lists import resolve_edm_lists
from canopy.converter.services.conversion_service import ConversionService

_CALC_LIST_TEXT = "CALC1\n\nsum\nA+B\n"
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def named_calc_edl(monkeypatch) -> bytes:
    """Return a screen whose text input reads the named calc ``CALC\\sum(PV:A, PV:B)``.

    ``EDMFILES`` and ``EDMCOLORFILE`` are unset so the parser's environment
    search cannot supply a calc list of its own.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The requesting test's ``monkeypatch`` fixture.

    Returns
    -------
    bytes
        The ``min.edl`` fixture with its setpoint PV replaced by the named calc.
    """
    monkeypatch.delenv("EDMFILES", raising=False)
    monkeypatch.delenv("EDMCOLORFILE", raising=False)
    text = (_FIXTURES / "min.edl").read_text(encoding="utf-8")
    return text.replace('"$(PREFIX):SETPOINT"', '"CALC\\\\sum(PV:A, PV:B)"').encode()


def _text_control_pv(screen: dict) -> str:
    """Return the ``pv`` prop of the widget converted from the fixture's text control.

    The fixture's ``activeXTextDspClass`` is not marked editable, so it converts to a
    read-only ``pv-label``.

    Parameters
    ----------
    screen : dict
        Screen IR wire dict from a `ConversionResult`.

    Returns
    -------
    str
        The text control's ``pv`` prop.
    """
    (widget,) = [child for child in screen["root"]["children"] if child["type"] == "pv-label"]
    return widget["props"]["pv"]


def test_converter_config_reads_calc_list_path_from_env(monkeypatch, tmp_path):
    """``CANOPY_CONVERTER__CALC_LIST_PATH`` populates ``calc_list_path`` as a ``Path``."""
    calc = tmp_path / "calc.list"
    monkeypatch.setenv("CANOPY_CONVERTER__CALC_LIST_PATH", str(calc))
    config = ConverterConfig()
    assert isinstance(config.calc_list_path, Path)
    assert config.calc_list_path == calc


def test_resolve_edm_lists_returns_a_configured_calc_list_of_any_name(tmp_path):
    """A configured file is used whatever it is called."""
    calc = tmp_path / "site-calcs.list"
    calc.write_text(_CALC_LIST_TEXT, encoding="utf-8")
    _, resolved = resolve_edm_lists(ConverterConfig(calc_list_path=calc))
    assert resolved == calc


def test_resolve_edm_lists_drops_a_missing_calc_list_with_a_warning(tmp_path, caplog):
    """A configured path with no file behind it resolves to ``None`` and logs a warning."""
    with caplog.at_level(logging.WARNING, logger=edm_lists.__name__):
        _, resolved = resolve_edm_lists(ConverterConfig(calc_list_path=tmp_path / "absent.list"))
    assert resolved is None
    assert any("absent.list" in record.getMessage() for record in caplog.records)


def test_resolve_edm_lists_falls_back_to_a_calc_list_beside_the_colors_list(tmp_path):
    """With no configured calc list, a ``calc.list`` beside the resolved ``colors.list`` is used."""
    colors = tmp_path / "colors.list"
    colors.write_text("", encoding="utf-8")
    beside = tmp_path / "calc.list"
    beside.write_text(_CALC_LIST_TEXT, encoding="utf-8")
    assert resolve_edm_lists(ConverterConfig(colors_list_path=colors)) == (colors, beside)


def test_resolve_edm_lists_bundled_colors_list_has_no_calc_list_beside_it():
    """The default config resolves the bundled palette and no calc list."""
    config = ConverterConfig()
    assert resolve_edm_lists(config) == (config.colors_list_path, None)


def test_conversion_translates_a_named_calc_from_the_explicit_calc_list(named_calc_edl, tmp_path):
    """A named calc resolves through a calc list passed explicitly, even one not called ``calc.list``.

    Without the calc list the PV is left untranslated; with it, the calc is
    hoisted into a formula and the widget reads that formula.
    """
    calc = tmp_path / "site-calcs.list"
    calc.write_text(_CALC_LIST_TEXT, encoding="utf-8")

    untranslated = ConversionService().convert(named_calc_edl, kind="edl")
    translated = ConversionService(calc_list_path=calc).convert(named_calc_edl, kind="edl")

    assert _text_control_pv(untranslated.screen) == "CALC\\sum(PV:A, PV:B)"
    assert untranslated.formulas == []
    (formula,) = translated.formulas
    assert _text_control_pv(translated.screen) == f"formula://{formula.name}"
    assert formula.expression == "A+B"
    assert formula.bindings == {"A": "PV:A", "B": "PV:B"}


async def test_converter_lifespan_hands_the_resolved_lists_to_the_service(monkeypatch, tmp_path):
    """Startup builds the bound service with the resolved lists and shutdown unbinds the gateway."""
    calc = tmp_path / "site-calcs.list"
    calc.write_text(_CALC_LIST_TEXT, encoding="utf-8")
    config = ConverterConfig(calc_list_path=calc)
    monkeypatch.setattr(converter_app, "get_converter_config", lambda: config)
    async with converter_app.converter_lifespan():
        service = gateway.get_service()
        assert isinstance(service, ConversionService)
        assert service._colors_list_path == config.colors_list_path
        assert service._calc_list_path == calc
    with pytest.raises(RuntimeError, match="before app startup"):
        gateway.get_service()
