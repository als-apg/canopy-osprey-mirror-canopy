"""Smoke tests for the Canopy package: boots, imports, basic endpoints."""

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


def test_docs_index_links_to_each_module() -> None:
    client = TestClient(app)
    response = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Canopy service" in response.text
    assert "<code>/</code>, <code>/health</code>" in response.text
    assert 'href="/openapi.json"' in response.text
    assert 'href="/docs/all">Aggregate Swagger UI</a>' in response.text
    assert 'href="/openapi-aggregate.json">Aggregate OpenAPI JSON</a>' in response.text
    for module in (
        "mole",
        "hare",
        "raccoon",
        "vole",
        "fox",
        "beaver",
        "squirrel",
        "magpie",
        "jobs",
        "converter",
        "analytics",
    ):
        assert f'href="/{module}/docs"' in response.text
        assert f'href="/{module}/openapi.json"' in response.text
        assert f"<code>/{module}/v1/...</code>" in response.text


def test_docs_index_respects_root_path() -> None:
    client = TestClient(app, root_path="/api/canopy")
    response = client.get("/docs")

    assert response.status_code == 200
    assert 'href="/api/canopy/docs"' in response.text
    assert 'href="/api/canopy/docs/all"' in response.text
    assert 'href="/api/canopy/openapi-aggregate.json"' in response.text
    assert 'href="/api/canopy/mole/docs"' in response.text
    assert 'href="/api/canopy/mole/openapi.json"' in response.text
    assert 'href="/api/canopy/openapi.json"' in response.text


def test_aggregate_openapi_includes_prefixed_module_paths_and_schemas() -> None:
    client = TestClient(app)
    response = client.get("/openapi-aggregate.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["servers"] == [{"url": "/"}]
    assert "/health" in schema["paths"]
    assert "/mole/v1/health/watchdog" in schema["paths"]
    assert "/hare/v1/health/watchdog" in schema["paths"]
    assert "MoleHeartbeatDTO" in schema["components"]["schemas"]
    assert "HareHeartbeatDTO" in schema["components"]["schemas"]
    assert "MoleAPIKeyHeader" in schema["components"]["securitySchemes"]
    assert schema["paths"]["/mole/v1/search"]["get"]["security"] == [{"MoleAPIKeyHeader": []}]
    assert (
        schema["paths"]["/mole/v1/health/watchdog"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/MoleHeartbeatDTO"
    )

    def assert_references_exist(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/components/"):
                _, _, section, name = reference.split("/", maxsplit=3)
                name = name.replace("~1", "/").replace("~0", "~")
                assert name in schema["components"][section]
            for item in value.values():
                assert_references_exist(item)
        elif isinstance(value, list):
            for item in value:
                assert_references_exist(item)

    assert_references_exist(schema)


def test_aggregate_docs_and_schema_respect_root_path() -> None:
    client = TestClient(app, root_path="/api/canopy")

    docs_response = client.get("/docs/all")
    schema_response = client.get("/openapi-aggregate.json")

    assert docs_response.status_code == 200
    assert "url: '/api/canopy/openapi-aggregate.json'" in docs_response.text
    assert schema_response.status_code == 200
    assert schema_response.json()["servers"] == [{"url": "/api/canopy"}]


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
