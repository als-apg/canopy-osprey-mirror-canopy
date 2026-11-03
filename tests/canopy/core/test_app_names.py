"""The shared client app-name rule (Magpie and E-log)."""

import pytest

from canopy.core.app_names import is_app


@pytest.mark.parametrize("name", ["squirrel", "trace", "react-trace", "app_2", "a", "x" * 64])
def test_valid_app_names(name):
    assert is_app(name)


@pytest.mark.parametrize("name", ["", "Squirrel", "has space", "a/b", "..", "x" * 65, "trace\n"])
def test_invalid_app_names(name):
    assert not is_app(name)
