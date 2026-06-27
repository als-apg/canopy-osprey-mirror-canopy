"""API tests for the PV registry + meta-PV endpoints."""

from httpx import AsyncClient

from tests.canopy.squirrel.conftest import READ_HEADERS, WRITE_HEADERS


async def test_create_pv_camelcase_wire(client: AsyncClient):
    r = await client.post(
        "/v1/pvs",
        headers=WRITE_HEADERS,
        json={"setpointAddress": "DEV:SP", "readbackAddress": "DEV:RB", "device": "DEV", "absTolerance": 0.5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["setpointAddress"] == "DEV:SP"
    assert body["absTolerance"] == 0.5
    assert {"id", "createdDate", "lastModifiedDate", "tags"} <= body.keys()


async def test_create_requires_an_address(client: AsyncClient):
    r = await client.post("/v1/pvs", headers=WRITE_HEADERS, json={"device": "NOADDR"})
    assert r.status_code == 422  # NewPVElementDTO.validate_at_least_one_address


async def test_duplicate_setpoint_conflicts(client: AsyncClient, sample_pv: dict):
    r = await client.post("/v1/pvs", headers=WRITE_HEADERS, json={"setpointAddress": sample_pv["setpointAddress"]})
    assert r.status_code == 409
    assert r.json()["code"] == "pv_already_exists"


async def test_search_and_devices(client: AsyncClient, sample_pv: dict):
    r = await client.get("/v1/pvs?pvName=TEST:PV", headers=READ_HEADERS)
    assert r.status_code == 200
    assert any(p["setpointAddress"] == "TEST:PV:001:SP" for p in r.json())

    r = await client.get("/v1/pvs/devices", headers=READ_HEADERS)
    assert r.status_code == 200 and "TEST-DEVICE-001" in r.json()


async def test_paged_and_filtered_search(client: AsyncClient, sample_pv: dict):
    r = await client.get("/v1/pvs/paged?pageSize=5", headers=READ_HEADERS)
    assert r.status_code == 200
    assert {"results", "continuationToken", "totalCount"} <= r.json().keys()

    r = await client.get("/v1/pvs/search?q=TEST&limit=10", headers=READ_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["totalCount"] >= 1 and body["limit"] == 10


async def test_paged_bad_tagfilters_is_400(client: AsyncClient):
    r = await client.get("/v1/pvs/paged?tagFilters=NOT_JSON", headers=READ_HEADERS)
    assert r.status_code == 400


async def test_paged_tag_filter(client: AsyncClient, sample_tag_group: dict):
    gid = sample_tag_group["id"]
    r = await client.post(f"/v1/tags/{gid}/tags", headers=WRITE_HEADERS, json={"name": "filterme"})
    tag_id = r.json()["group"]["tags"][0]["id"]
    await client.post("/v1/pvs", headers=WRITE_HEADERS, json={"setpointAddress": "TAGGED:SP", "tags": [tag_id]})
    await client.post("/v1/pvs", headers=WRITE_HEADERS, json={"setpointAddress": "UNTAGGED:SP"})

    import json

    r = await client.get(
        f"/v1/pvs/paged?tagFilters={json.dumps({gid: [tag_id]})}", headers=READ_HEADERS
    )
    assert r.status_code == 200
    addrs = {p["setpointAddress"] for p in r.json()["results"]}
    assert addrs == {"TAGGED:SP"}


async def test_multi_create(client: AsyncClient):
    data = [{"setpointAddress": f"BULK:{i}:SP", "device": f"D{i}"} for i in range(3)]
    r = await client.post("/v1/pvs/multi", headers=WRITE_HEADERS, json=data)
    assert r.status_code == 200 and len(r.json()) == 3


async def test_update_and_delete_pv(client: AsyncClient, sample_pv: dict):
    pid = sample_pv["id"]
    r = await client.put(f"/v1/pvs/{pid}", headers=WRITE_HEADERS, json={"description": "updated"})
    assert r.status_code == 200 and r.json()["description"] == "updated"

    r = await client.delete(f"/v1/pvs/{pid}", headers=WRITE_HEADERS)
    assert r.status_code == 200
    # second delete -> 404
    assert (await client.delete(f"/v1/pvs/{pid}", headers=WRITE_HEADERS)).status_code == 404


async def test_update_bad_uuid_is_404(client: AsyncClient):
    r = await client.put("/v1/pvs/not-a-uuid", headers=WRITE_HEADERS, json={"description": "x"})
    assert r.status_code == 404
    assert r.json()["code"] == "pv_not_found"


# --- meta PVs --------------------------------------------------------------- #


async def test_meta_pv_lifecycle(client: AsyncClient, sample_pv: dict):
    pid = sample_pv["id"]
    # promote
    r = await client.post("/v1/pvs/meta", headers=WRITE_HEADERS, json={"pvName": "TEST:PV:001:SP", "displayName": "Meta"})
    assert r.status_code == 200, r.text
    assert r.json()["pvId"] == pid and r.json()["isActive"] is True

    # duplicate promote -> 409
    assert (await client.post("/v1/pvs/meta", headers=WRITE_HEADERS, json={"pvName": "TEST:PV:001:SP"})).status_code == 409

    # list + get
    r = await client.get("/v1/pvs/meta", headers=READ_HEADERS)
    assert any(m["pvId"] == pid and m["pvName"] for m in r.json())
    assert (await client.get(f"/v1/pvs/meta/{pid}", headers=READ_HEADERS)).status_code == 200

    # patch display name + deactivate
    r = await client.patch(f"/v1/pvs/meta/{pid}", headers=WRITE_HEADERS, json={"displayName": "Renamed", "active": False})
    assert r.status_code == 200 and r.json()["displayName"] == "Renamed" and r.json()["isActive"] is False

    # filter by active
    r = await client.get("/v1/pvs/meta?isActive=true", headers=READ_HEADERS)
    assert all(m["pvId"] != pid for m in r.json())

    # delete -> 204
    assert (await client.delete(f"/v1/pvs/meta/{pid}", headers=WRITE_HEADERS)).status_code == 204
    assert (await client.get(f"/v1/pvs/meta/{pid}", headers=READ_HEADERS)).status_code == 404


async def test_add_meta_for_unknown_pv_is_404(client: AsyncClient):
    r = await client.post("/v1/pvs/meta", headers=WRITE_HEADERS, json={"pvName": "DOES:NOT:EXIST"})
    assert r.status_code == 404
    assert r.json()["code"] == "pv_not_found"
