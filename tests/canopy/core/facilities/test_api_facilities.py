"""API tests for the facility endpoints.

Reads are read-gated, writes require the facility wildcard (stricter than plain
``admin``, since editing the hierarchy can widen what an existing grant resolves
to); the ``fake_keys`` fixture binds a read key and a wildcard-facility admin key
so both halves can be shown against the same router.
"""

from canopy.core.facilities.models import Facility

READ_HEADERS = {"X-API-Key": "test-read"}
ADMIN_HEADERS = {"X-API-Key": "test-admin"}
SCOPED_ADMIN_HEADERS = {"X-API-Key": "test-scoped-admin"}


async def _seed_facility(session, slug: str, parent_id: str | None = None) -> Facility:
    facility = Facility(slug=slug, name=slug.upper(), parent_id=parent_id)
    session.add(facility)
    await session.commit()
    return facility


async def test_list_facilities(facilities_client, session, session_maker, fake_keys):
    lcls = await _seed_facility(session, "lcls")
    await _seed_facility(session, "lcls-sc", parent_id=lcls.id)

    r = facilities_client.get("/v1/facilities", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    slugs = {f["slug"] for f in r.json()}
    assert {"lcls", "lcls-sc"} <= slugs
    sc = next(f for f in r.json() if f["slug"] == "lcls-sc")
    assert sc["parentId"] == lcls.id


async def test_facilities_require_read_auth(facilities_client):
    assert facilities_client.get("/v1/facilities").status_code == 401


async def test_admin_only_key_with_no_read_can_still_list(facilities_client, session, session_maker, fake_keys):
    """An admin-only provisioning key — no ``read`` at all — still needs to see
    what it just created; ``GET`` accepts ``admin`` as an alternative to ``read``
    rather than forcing every provisioning key to carry an unrelated data scope."""
    await _seed_facility(session, "lcls")

    r = facilities_client.get("/v1/facilities", headers={"X-API-Key": "test-admin-only"})

    assert r.status_code == 200, r.text
    assert [f["slug"] for f in r.json()] == ["lcls"]


async def test_create_then_read_back(facilities_client, session_maker, fake_keys):
    created = facilities_client.post(
        "/v1/facilities",
        json={"slug": "lcls", "name": "LCLS", "description": "Linac Coherent Light Source"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    assert created.json()["slug"] == "lcls"
    assert created.json()["parentId"] is None

    listed = facilities_client.get("/v1/facilities", headers=READ_HEADERS)
    assert [f["slug"] for f in listed.json()] == ["lcls"]


async def test_create_child_by_parent_slug(facilities_client, session_maker, fake_keys):
    parent = facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)
    child = facilities_client.post(
        "/v1/facilities",
        json={"slug": "lcls-sc", "name": "LCLS-II SC", "parent": "lcls"},
        headers=ADMIN_HEADERS,
    )

    assert child.status_code == 200, child.text
    # The request names the parent by slug; the response reports it by id, which is
    # what a client walks to assemble the tree.
    assert child.json()["parentId"] == parent.json()["id"]


async def test_create_rejects_unknown_parent_slug(facilities_client, session_maker, fake_keys):
    r = facilities_client.post(
        "/v1/facilities",
        json={"slug": "orphan", "name": "Orphan", "parent": "no-such-parent"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 404, r.text
    assert "no-such-parent" in r.json()["detail"]


async def test_create_rejects_duplicate_slug(facilities_client, session_maker, fake_keys):
    facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)
    r = facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "Again"}, headers=ADMIN_HEADERS)

    assert r.status_code == 409, r.text


async def test_create_rejects_uppercase_slug_rather_than_lowercasing_it(facilities_client, session_maker, fake_keys):
    """``LCLS`` is a 422. Silently normalizing would hand back a slug that doesn't
    match what the operator then types into a key grant."""
    r = facilities_client.post("/v1/facilities", json={"slug": "LCLS", "name": "LCLS"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422, r.text

    listed = facilities_client.get("/v1/facilities", headers=READ_HEADERS)
    assert listed.json() == []


async def test_create_rejects_leading_hyphen_slug(facilities_client, session_maker, fake_keys):
    r = facilities_client.post("/v1/facilities", json={"slug": "-bad", "name": "Bad"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422, r.text


async def test_create_rejects_trailing_hyphen_slug(facilities_client, session_maker, fake_keys):
    r = facilities_client.post("/v1/facilities", json={"slug": "bad-", "name": "Bad"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422, r.text


async def test_create_rejects_doubled_hyphen_slug(facilities_client, session_maker, fake_keys):
    r = facilities_client.post("/v1/facilities", json={"slug": "a--b", "name": "Bad"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422, r.text


async def test_create_rejects_a_slug_reserved_by_react_squirrels_routes(facilities_client, session_maker, fake_keys):
    """``snapshots`` collides with react-squirrel's static `/{facility}/snapshots`
    page route (slaclab/react-squirrel#208) and would be unreachable through the
    frontend if allowed."""
    r = facilities_client.post("/v1/facilities", json={"slug": "snapshots", "name": "Snapshots"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422, r.text


async def test_update_name_and_reparent_by_slug(facilities_client, session_maker, fake_keys):
    facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)
    lcls2 = facilities_client.post("/v1/facilities", json={"slug": "lcls2", "name": "LCLS 2"}, headers=ADMIN_HEADERS)
    facilities_client.post(
        "/v1/facilities", json={"slug": "lcls-sc", "name": "SC", "parent": "lcls"}, headers=ADMIN_HEADERS
    )

    r = facilities_client.put(
        "/v1/facilities/lcls-sc",
        json={"name": "LCLS-II Superconducting", "parent": "lcls2"},
        headers=ADMIN_HEADERS,
    )

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "LCLS-II Superconducting"
    assert r.json()["parentId"] == lcls2.json()["id"]
    assert r.json()["slug"] == "lcls-sc"


async def test_update_unknown_slug_is_404(facilities_client, session_maker, fake_keys):
    r = facilities_client.put("/v1/facilities/nope", json={"name": "Nope"}, headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text


async def test_delete_removes_the_facility(facilities_client, session_maker, fake_keys):
    facilities_client.post("/v1/facilities", json={"slug": "doomed", "name": "Doomed"}, headers=ADMIN_HEADERS)

    r = facilities_client.delete("/v1/facilities/doomed", headers=ADMIN_HEADERS)

    assert r.status_code == 200, r.text
    assert facilities_client.get("/v1/facilities", headers=READ_HEADERS).json() == []


async def test_delete_unknown_slug_is_404(facilities_client, session_maker, fake_keys):
    r = facilities_client.delete("/v1/facilities/nope", headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text


async def test_delete_refused_while_it_has_children(facilities_client, session_maker, fake_keys):
    facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)
    facilities_client.post(
        "/v1/facilities", json={"slug": "lcls-sc", "name": "SC", "parent": "lcls"}, headers=ADMIN_HEADERS
    )

    r = facilities_client.delete("/v1/facilities/lcls", headers=ADMIN_HEADERS)

    # 400, not 409 — `FacilityHasChildren`'s shipped status code.
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "facility_has_children"
    assert facilities_client.delete("/v1/facilities/lcls-sc", headers=ADMIN_HEADERS).status_code == 200
    assert facilities_client.delete("/v1/facilities/lcls", headers=ADMIN_HEADERS).status_code == 200


async def test_read_key_may_list_but_not_write(facilities_client, session_maker, fake_keys):
    """Facility *existence* isn't a secret, but defining one is an admin act — a
    ``read`` key (and equally a ``write`` key, which this one also isn't) is refused
    all three write verbs while the ``GET`` it's entitled to keeps working."""
    facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)

    assert (
        facilities_client.post("/v1/facilities", json={"slug": "x", "name": "X"}, headers=READ_HEADERS).status_code
        == 403
    )
    assert facilities_client.put("/v1/facilities/lcls", json={"name": "Nope"}, headers=READ_HEADERS).status_code == 403
    assert facilities_client.delete("/v1/facilities/lcls", headers=READ_HEADERS).status_code == 403


async def test_admin_without_facility_wildcard_may_list_but_not_write(facilities_client, session_maker, fake_keys):
    """Writing the hierarchy needs the facility wildcard, not just ``admin`` —
    stricter than key management, since reparenting or deleting a node can widen
    what an *existing* grant on an ancestor resolves to. A plain admin key (no
    facility grant) is refused all three write verbs, same as a read key, while its
    ``GET`` still works."""
    facilities_client.post("/v1/facilities", json={"slug": "lcls", "name": "LCLS"}, headers=ADMIN_HEADERS)

    denied_create = facilities_client.post(
        "/v1/facilities", json={"slug": "x", "name": "X"}, headers=SCOPED_ADMIN_HEADERS
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["code"] == "insufficient_facility_access"
    assert (
        facilities_client.put("/v1/facilities/lcls", json={"name": "Nope"}, headers=SCOPED_ADMIN_HEADERS).status_code
        == 403
    )
    assert facilities_client.delete("/v1/facilities/lcls", headers=SCOPED_ADMIN_HEADERS).status_code == 403

    listed = facilities_client.get("/v1/facilities", headers=SCOPED_ADMIN_HEADERS)
    assert listed.status_code == 200
    assert [f["slug"] for f in listed.json()] == ["lcls"]

    listed = facilities_client.get("/v1/facilities", headers=READ_HEADERS)
    assert listed.status_code == 200
    assert [f["name"] for f in listed.json()] == ["LCLS"]
