"""Smoke tests for the Canopy package — boots, imports, basic endpoints."""

from fastapi.testclient import TestClient

from canopy.main import app


def test_canopy_imports() -> None:
    import canopy

    assert hasattr(canopy, "__version__")


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Canopy"
    assert "version" in body


def test_unhandled_exception_returns_slug_envelope() -> None:
    """The fallback exception handler returns the {code, detail} shape."""
    if not any(getattr(r, "path", None) == "/__test/boom" for r in app.routes):

        @app.get("/__test/boom")
        async def _boom() -> None:
            raise RuntimeError("kaboom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/__test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "unhandled_exception"
    assert "detail" in body
