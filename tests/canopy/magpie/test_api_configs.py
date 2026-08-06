"""API tests for the magpie config store.

asyncio_mode = "auto" (pyproject) — no per-test decorator needed.
"""

from httpx import AsyncClient

from tests.canopy.magpie.conftest import READ_HEADERS, WRITE_HEADERS


async def test_create_and_resolve_roundtrip(client: AsyncClient):
    data = {"pvs": ["A", "B"], "timeRange": {"start": "-1h", "end": "now"}}
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={"name": "ws", "data": data, "createdBy": "me"},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    slug = created["slug"]
    assert created["app"] == "trace"
    assert created["data"] == data
    assert len(slug) == 10

    # Resolve the shareable-link path.
    r = await client.get(f"/v1/configs/trace/{slug}", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    fetched = r.json()
    assert fetched["slug"] == slug
    assert fetched["data"] == data


async def test_camelcase_wire(client: AsyncClient, sample_config: dict):
    # Response uses camelCase timestamps + fields.
    assert "createdDate" in sample_config
    assert "lastModifiedDate" in sample_config
    assert sample_config["createdBy"] == "tester"
    assert "data" in sample_config


async def test_app_handshake_mismatch_is_404(client: AsyncClient, sample_config: dict):
    """A config created under 'trace' must not be resolvable under another app."""
    slug = sample_config["slug"]
    r = await client.get(f"/v1/configs/beaver/{slug}", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "config_not_found"

    # And it IS resolvable under its own app.
    r = await client.get(f"/v1/configs/trace/{slug}", headers=READ_HEADERS)
    assert r.status_code == 200


async def test_list_scoped_by_app(client: AsyncClient):
    for app, name in [("trace", "t1"), ("trace", "t2"), ("beaver", "b1")]:
        r = await client.post(f"/v1/configs/{app}", headers=WRITE_HEADERS, json={"name": name, "data": {}})
        assert r.status_code == 200, r.text

    r = await client.get("/v1/configs/trace", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totalCount"] == 2
    assert {c["name"] for c in body["results"]} == {"t1", "t2"}
    assert all(c["app"] == "trace" for c in body["results"])


async def test_list_pagination(client: AsyncClient):
    for i in range(3):
        r = await client.post("/v1/configs/trace", headers=WRITE_HEADERS, json={"name": f"c{i}", "data": {}})
        assert r.status_code == 200, r.text

    r = await client.get("/v1/configs/trace?pageSize=2", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert len(page1["results"]) == 2
    assert page1["totalCount"] == 3
    token = page1["continuationToken"]
    assert token

    r = await client.get(f"/v1/configs/trace?pageSize=2&continuationToken={token}", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    page2 = r.json()
    assert len(page2["results"]) == 1
    assert page2["continuationToken"] is None


async def test_get_missing_slug_404(client: AsyncClient):
    r = await client.get("/v1/configs/trace/nonexistent", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "config_not_found"


async def test_get_malformed_slug_404(client: AsyncClient):
    r = await client.get("/v1/configs/trace/not-a-valid-slug!!", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "config_not_found"


async def test_malformed_continuation_token_400(client: AsyncClient):
    """A bad token must not reach asyncpg as an invalid UUID comparison (500)."""
    for token in ("garbage", "0" * 40, "not-a-uuid-but-36-chars-long-aaaaaaaa"):
        r = await client.get(f"/v1/configs/trace?continuationToken={token}", headers=READ_HEADERS)
        assert r.status_code == 400, f"{token!r} -> {r.status_code} {r.text}"
        assert r.json()["code"] == "bad_request"


async def test_malformed_app_name_is_invalid_app_name(client: AsyncClient):
    """A bad app name gets its own code, and the message states the rule.

    Distinguishable from the other 400s on this surface (bad token, NUL, oversize)
    without parsing ``detail``.
    """
    for app in ("Trace", "my.app", "trace ", "a" * 65, "trace!"):
        r = await client.get(f"/v1/configs/{app}", headers=READ_HEADERS)
        assert r.status_code == 400, f"{app!r} -> {r.status_code} {r.text}"
        body = r.json()
        assert body["code"] == "invalid_app_name", body
        assert "lowercase" in body["detail"], body


async def test_malformed_app_name_on_create_is_invalid_app_name(client: AsyncClient):
    r = await client.post("/v1/configs/Trace", headers=WRITE_HEADERS, json={"name": "x", "data": {}})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_app_name"


async def test_item_routes_treat_bad_app_as_not_found(client: AsyncClient, sample_config: dict):
    """``app`` is an assertion on the item routes, so a bad one reads as 404, not 400.

    Deliberate asymmetry with create/list, which write or filter on ``app``.
    """
    r = await client.get(f"/v1/configs/Trace/{sample_config['slug']}", headers=READ_HEADERS)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "config_not_found"


async def test_nul_byte_in_data_is_400(client: AsyncClient):
    """Legal JSON that Postgres can't store must be a clean 400, not a 500."""
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={"name": "nul", "data": {"a": "b\u0000c"}},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "bad_request"


async def test_nul_byte_in_name_is_400(client: AsyncClient):
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={"name": "a\u0000b", "data": {}},
    )
    assert r.status_code == 400, r.text


async def test_non_ascii_data_measured_as_utf8(client: AsyncClient):
    """Size is capped in real UTF-8 bytes, not \\uXXXX-escaped ones.

    60k accented chars is ~120 KB of UTF-8 — comfortably under the 256 KB cap, but
    ~360 KB once ``json.dumps`` escapes each one, which used to reject it.
    """
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={"name": "accented", "data": {"blob": "é" * 60_000}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["blob"] == "é" * 60_000


async def test_oversize_data_still_rejected(client: AsyncClient):
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={"name": "huge", "data": {"blob": "a" * (256 * 1024 + 100)}},
    )
    assert r.status_code == 400, r.text
    assert "limit" in r.json()["detail"]


async def test_update_mutable_fields(client: AsyncClient, sample_config: dict):
    slug = sample_config["slug"]
    r = await client.put(
        f"/v1/configs/trace/{slug}",
        headers=WRITE_HEADERS,
        json={"name": "renamed", "data": {"pvs": ["X"]}},
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["name"] == "renamed"
    assert updated["data"] == {"pvs": ["X"]}
    # Immutable identity preserved.
    assert updated["slug"] == slug
    assert updated["app"] == "trace"


async def test_update_wrong_app_404(client: AsyncClient, sample_config: dict):
    slug = sample_config["slug"]
    r = await client.put(f"/v1/configs/beaver/{slug}", headers=WRITE_HEADERS, json={"name": "x"})
    assert r.status_code == 404


async def test_delete(client: AsyncClient, sample_config: dict):
    slug = sample_config["slug"]
    r = await client.delete(f"/v1/configs/trace/{slug}", headers=WRITE_HEADERS)
    assert r.status_code == 200
    assert r.json() is True

    r = await client.get(f"/v1/configs/trace/{slug}", headers=READ_HEADERS)
    assert r.status_code == 404


async def test_delete_wrong_app_404(client: AsyncClient, sample_config: dict):
    slug = sample_config["slug"]
    r = await client.delete(f"/v1/configs/beaver/{slug}", headers=WRITE_HEADERS)
    assert r.status_code == 404


# --- auth --------------------------------------------------------------------- #


async def test_missing_key_401(client: AsyncClient):
    r = await client.get("/v1/configs/trace")
    assert r.status_code == 401


async def test_read_key_on_write_route_403(client: AsyncClient):
    r = await client.post("/v1/configs/trace", headers=READ_HEADERS, json={"name": "x", "data": {}})
    assert r.status_code == 403
