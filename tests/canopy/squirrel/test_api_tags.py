"""API tests for the tag / tag-group endpoints."""

from httpx import AsyncClient

from tests.canopy.squirrel.conftest import READ_HEADERS, WRITE_HEADERS


async def test_create_and_list_groups(client: AsyncClient):
    r = await client.post("/v1/tags", headers=WRITE_HEADERS, json={"name": "Location"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Location"
    assert "createdDate" in body and "lastModifiedDate" in body  # camelCase wire

    r = await client.get("/v1/tags", headers=READ_HEADERS)
    assert r.status_code == 200
    groups = r.json()
    assert any(g["name"] == "Location" and g["tagCount"] == 0 for g in groups)


async def test_duplicate_group_conflicts(client: AsyncClient, sample_tag_group: dict):
    r = await client.post("/v1/tags", headers=WRITE_HEADERS, json={"name": sample_tag_group["name"]})
    assert r.status_code == 409
    assert r.json()["code"] == "tag_group_already_exists"


async def test_get_group_bad_uuid_is_404(client: AsyncClient):
    r = await client.get("/v1/tags/not-a-uuid", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "tag_group_not_found"


async def test_get_group_wraps_in_list(client: AsyncClient, sample_tag_group: dict):
    r = await client.get(f"/v1/tags/{sample_tag_group['id']}", headers=READ_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["id"] == sample_tag_group["id"]


async def test_add_tag_and_duplicate(client: AsyncClient, sample_tag_group: dict):
    gid = sample_tag_group["id"]
    r = await client.post(f"/v1/tags/{gid}/tags", headers=WRITE_HEADERS, json={"name": "Bldg-A"})
    assert r.status_code == 200, r.text
    assert r.json()["wasCreated"] is True
    tags = r.json()["group"]["tags"]
    assert len(tags) == 1 and tags[0]["name"] == "Bldg-A"

    # Duplicate without skip -> 409; with skip -> no-op success.
    r = await client.post(f"/v1/tags/{gid}/tags", headers=WRITE_HEADERS, json={"name": "Bldg-A"})
    assert r.status_code == 409
    r = await client.post(f"/v1/tags/{gid}/tags?skip_duplicates=true", headers=WRITE_HEADERS, json={"name": "Bldg-A"})
    assert r.status_code == 200 and r.json()["wasCreated"] is False


async def test_update_and_remove_tag(client: AsyncClient, sample_tag_group: dict):
    gid = sample_tag_group["id"]
    r = await client.post(f"/v1/tags/{gid}/tags", headers=WRITE_HEADERS, json={"name": "t1"})
    tag_id = r.json()["group"]["tags"][0]["id"]

    r = await client.put(f"/v1/tags/{gid}/tags/{tag_id}", headers=WRITE_HEADERS, json={"name": "t1-renamed"})
    assert r.status_code == 200
    assert r.json()["tags"][0]["name"] == "t1-renamed"

    r = await client.delete(f"/v1/tags/{gid}/tags/{tag_id}", headers=WRITE_HEADERS)
    assert r.status_code == 200
    assert r.json()["tags"] == []


async def test_delete_group(client: AsyncClient, sample_tag_group: dict):
    r = await client.delete(f"/v1/tags/{sample_tag_group['id']}", headers=WRITE_HEADERS)
    assert r.status_code == 200
    r = await client.get(f"/v1/tags/{sample_tag_group['id']}", headers=READ_HEADERS)
    assert r.status_code == 404


async def test_bulk_import(client: AsyncClient):
    payload = {"groups": {"GrpA": ["a1", "a2"], "GrpB": ["b1"]}}
    r = await client.post("/v1/tags/bulk", headers=WRITE_HEADERS, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["groupsCreated"] == 2
    assert body["tagsCreated"] == 3
    assert body["tagsSkipped"] == 0

    # Re-import overlapping data -> existing group reused, dup tags skipped (not errors).
    r = await client.post("/v1/tags/bulk", headers=WRITE_HEADERS, json={"groups": {"GrpA": ["a1", "a3"]}})
    body = r.json()
    assert body["groupsCreated"] == 0
    assert body["tagsCreated"] == 1  # a3
    assert body["tagsSkipped"] == 1  # a1


async def test_auth_gating(client: AsyncClient):
    assert (await client.get("/v1/tags")).status_code in (401, 403)  # no key
    r = await client.post("/v1/tags", headers=READ_HEADERS, json={"name": "x"})  # read-only key
    assert r.status_code == 403
