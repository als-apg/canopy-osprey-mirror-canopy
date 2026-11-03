"""Formula Engine configuration tests."""

import os
import logging

import pytest

from canopy.formula_engine.config import (
    LEGACY_ENV_PREFIX,
    FormulaEngineConfig,
    warn_legacy_env,
)


@pytest.fixture(autouse=True)
def _no_inherited_legacy_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(LEGACY_ENV_PREFIX):
            monkeypatch.delenv(name)


def test_legacy_fox_env_vars_are_named_in_one_warning(monkeypatch, caplog):
    monkeypatch.setenv("CANOPY_FOX__MAX_OUTPUT_POINTS", "500")
    monkeypatch.setenv("CANOPY_FOX__EVAL_TIMEOUT_MS", "1000")

    with caplog.at_level(logging.WARNING, logger="canopy.formula_engine.config"):
        warn_legacy_env()

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "CANOPY_FOX__EVAL_TIMEOUT_MS, CANOPY_FOX__MAX_OUTPUT_POINTS" in message
    assert "CANOPY_FORMULA_ENGINE__" in message
    # And they really are ignored.
    assert FormulaEngineConfig(_env_file=None).eval_timeout_ms == 250


def test_no_warning_without_legacy_env_vars(monkeypatch, caplog):
    monkeypatch.setenv("CANOPY_FORMULA_ENGINE__EVAL_TIMEOUT_MS", "1000")
    monkeypatch.setenv("CANOPY_FOXY__SETTING", "1")

    with caplog.at_level(logging.WARNING, logger="canopy.formula_engine.config"):
        warn_legacy_env()

    assert caplog.records == []
