"""Smoke tests for the Canopy package: boots, imports, basic endpoints."""

import json

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


def test_docs_ui_lists_main_and_module_specs() -> None:
    client = TestClient(app)
    response = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    urls_line = next(line.strip() for line in response.text.splitlines() if line.strip().startswith("urls: "))
    urls = json.loads(urls_line.removeprefix("urls: ").rstrip(","))

    assert "    url: " not in response.text
    assert urls[0] == {"url": "/openapi.json", "name": "Canopy"}
    assert urls[1:] == [
        {"url": f"/{module}/openapi.json", "name": name}
        for module, name in (
            ("mole", "Mole"),
            ("hare", "Hare"),
            ("raccoon", "Raccoon"),
            ("vole", "Vole"),
            ("fox", "Fox"),
            ("beaver", "Beaver"),
            ("squirrel", "Squirrel"),
            ("magpie", "Magpie"),
            ("jobs", "Jobs"),
            ("converter", "Converter"),
            ("analytics", "Analytics"),
        )
    ]
    assert '"urls.primaryName": "Canopy"' in response.text
    assert "validatorUrl: null" in response.text
    assert "swagger-ui-standalone-preset.js" in response.text
    assert 'layout: "StandaloneLayout"' in response.text


def test_docs_ui_respects_root_path_for_specs() -> None:
    client = TestClient(app, root_path="/api/canopy")
    response = client.get("/docs")

    assert response.status_code == 200
    urls_line = next(line.strip() for line in response.text.splitlines() if line.strip().startswith("urls: "))
    urls = json.loads(urls_line.removeprefix("urls: ").rstrip(","))
    assert urls[0] == {"url": "/api/canopy/openapi.json", "name": "Canopy"}
    assert urls[1] == {"url": "/api/canopy/mole/openapi.json", "name": "Mole"}
    assert "    url: " not in response.text
    assert 'layout: "StandaloneLayout"' in response.text


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
