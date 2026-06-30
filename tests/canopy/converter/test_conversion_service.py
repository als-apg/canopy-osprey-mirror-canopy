"""Unit tests for the pure ConversionService (no HTTP layer)."""

import logging
from pathlib import Path

import pytest

from canopy.converter.errors import ConversionFailed, UnsupportedInput
from canopy.converter.services import conversion_service as cs
from canopy.converter.services.conversion_service import ConversionService

FIXTURES = Path(__file__).parent / "fixtures"


def _edl_bytes() -> bytes:
    return (FIXTURES / "p0_min.edl").read_bytes()


def test_convert_edl_bytes_produces_validated_ir():
    result = ConversionService().convert(_edl_bytes(), kind="edl")
    assert result.screen["schemaVersion"] == "1.0"
    assert result.screen["id"]
    assert result.stats.widget_count >= 1
    # p0_min carries an unmapped activeRectangleClass -> placeholder + warning (D11).
    assert any("placeholder" in w for w in result.warnings)


def test_unknown_widgets_are_grouped_by_original_class():
    result = ConversionService().convert(_edl_bytes(), kind="edl")
    classes = {u.original_class for u in result.unknown_widgets}
    assert "activeRectangleClass" in classes


def test_bad_kind_raises_unsupported_input():
    with pytest.raises(UnsupportedInput):
        ConversionService().convert(b"whatever", kind="bogus")  # type: ignore[arg-type]


def test_unexpected_failure_maps_to_conversion_failed_and_logs(monkeypatch, caplog):
    """A non-ValueError from convert_bytes stays a 422 (ConversionFailed) but is logged.

    The catch is intentionally broad (pydmconverter uses ValueError/KeyError for both
    bad input and bugs), so an unexpected error must still leave a traceback in the log.
    """

    def _boom(*_args, **_kwargs):
        raise KeyError("widget-type")

    monkeypatch.setattr(cs, "convert_bytes", _boom)
    with caplog.at_level(logging.ERROR, logger=cs.logger.name):
        with pytest.raises(ConversionFailed):
            ConversionService().convert(b"data", kind="edl")
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)
