"""Adapter errors surface as the standard `{code, detail}` JSON envelope."""

from tests.conftest import READ_HEADERS
from canopy.raccoon.adapters.base import DirectoryUnavailable


def test_directory_unavailable_returns_503(raccoon_client, fake_adapter):
    fake_adapter.error = DirectoryUnavailable("upstream is down")

    r = raccoon_client.get(
        "/raccoon/v1/pvs",
        params={"pattern": "X"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "directory_unavailable"
    assert "upstream is down" in body["detail"]


def test_adapter_misconfigured_returns_500(raccoon_client, directory_service, fake_adapter):
    # Drop the adapter from the registry to mimic an unconfigured deployment.
    directory_service._registry._adapter = None  # noqa: SLF001
    r = raccoon_client.get(
        "/raccoon/v1/pvs",
        params={"pattern": "X"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 500
    assert r.json()["code"] == "adapter_misconfigured"
