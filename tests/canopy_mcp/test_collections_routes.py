"""The collections HTTP surface: /collections, /collections/{id} and /collections-state."""

import os
import json
import errno
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.collections import (
    MAX_REFS,
    STATE_OPS,
    MAX_PINNED,
    MAX_RECENT,
    STATE_FILE,
    MAX_PUT_BODY_BYTES,
    MAX_STATE_OP_BYTES,
    MAX_POST_BODY_BYTES,
    MAX_COLLECTION_BYTES,
    MAX_RECENT_MANY_OP_BYTES,
)
from canopy_mcp.shared_library import library_config

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
RESPONSES = Path(__file__).parent / "fixtures" / "collections" / "responses"
JSON = {"content-type": "application/json"}
CREATE = {"if-none-match": "*"}
SCREEN = {"screen": "library/sr/overview"}
DRAFT = {"screen": "drafts/bts/view"}
TOOL = {"tool": "trace", "query": "pv=A:B&range=-1h", "name": "Beam current"}
BAD_IDS = ["has space", "a.b", "x" * 65, "a/b", "%2E%2E"]


def _collection(name="Injector", members=None, **extra):
    return {"name": name, "members": [SCREEN] if members is None else members, **extra}


def _write(directory: Path, name: str, doc) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def drafts(tmp_path: Path) -> Path:
    root = tmp_path / "drafts"
    (root / "screens").mkdir(parents=True)
    return root


@pytest.fixture
def facility(tmp_path: Path) -> Path:
    return tmp_path / "facility"


@pytest.fixture
def personal(drafts: Path) -> Path:
    # The default personal dir: nothing configures it.
    return drafts / "collections"


@pytest.fixture
def settings(drafts: Path, facility: Path) -> Settings:
    return Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, facility_collections_dir=facility)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


def _put(client: TestClient, collection_id: str, doc, headers: dict | None = None):
    return client.put(f"/collections/{collection_id}", content=json.dumps(doc), headers={**JSON, **(headers or {})})


def _create(client: TestClient, collection_id: str, doc, headers: dict | None = None):
    """A create-only PUT (``If-None-Match: *``)."""
    return _put(client, collection_id, doc, {**CREATE, **(headers or {})})


def _match(rev: str) -> dict:
    return {"if-match": rev}


def _op(client: TestClient, op: dict, headers: dict | None = None):
    return client.post("/collections-state", content=json.dumps(op), headers={**JSON, **(headers or {})})


def _fixture(name: str):
    return json.loads((RESPONSES / name).read_text(encoding="utf-8"))


def _is_error(r, status: int, fixture: str) -> dict:
    """The response is ``status`` with no-cache and exactly the body of ``responses/<fixture>``."""
    assert r.status_code == status, r.text
    assert r.headers["cache-control"] == "no-cache"
    body = r.json()
    assert set(body) <= {"code", "detail"} and isinstance(body["code"], str)
    assert body == _fixture(fixture)
    return body


# --- GET /collections ------------------------------------------------------------


def test_empty_stores_list_nothing(client: TestClient, personal: Path) -> None:
    r = client.get("/collections")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.json() == {"collections": []}
    assert not personal.exists()  # a read creates nothing


def test_list_is_facility_first_then_personal_each_sorted(client: TestClient, facility: Path) -> None:
    _write(facility, "b.json", _collection("beta", default=True))
    _write(facility, "a.json", _collection("Beta"))
    _write(facility, "z.json", _collection("alpha"))
    assert _create(client, "p2", _collection("Zulu")).status_code == 201
    assert _create(client, "p1", _collection("echo", [DRAFT, TOOL])).status_code == 201
    listed = client.get("/collections").json()["collections"]
    assert [(c["source"], c["id"]) for c in listed] == [
        ("facility", "z"),
        ("facility", "a"),
        ("facility", "b"),
        ("personal", "p1"),
        ("personal", "p2"),
    ]
    assert [c["ref"] for c in listed] == ["facility:z", "facility:a", "facility:b", "personal:p1", "personal:p2"]
    assert listed[2]["default"] is True
    assert listed[3]["members"] == [DRAFT, TOOL]
    assert all(isinstance(c["updated"], str) for c in listed)


def test_list_skips_a_bad_facility_file_and_the_state_file(client: TestClient, facility: Path) -> None:
    _write(facility, "good.json", _collection("Good"))
    (facility / "broken.json").write_text("{not json")
    _write(facility, "personal-only.json", _collection("Drafts", [DRAFT]))
    assert _op(client, {"op": "tick", "ref": "facility:good"}).status_code == 200
    listed = client.get("/collections").json()["collections"]
    assert [c["ref"] for c in listed] == ["facility:good"]


def test_no_facility_dir_configured_lists_personal_only(drafts: Path) -> None:
    s = Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST)
    with TestClient(create_app(s, startup_hooks=[])) as c:
        assert _create(c, "mine", _collection("Mine")).status_code == 201
        assert [x["ref"] for x in c.get("/collections").json()["collections"]] == ["personal:mine"]


def test_configured_collections_dir_is_the_personal_store(drafts: Path, tmp_path: Path) -> None:
    mine = tmp_path / "elsewhere"
    s = Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, collections_dir=mine)
    with TestClient(create_app(s, startup_hooks=[])) as c:
        assert _create(c, "mine", _collection("Mine")).status_code == 201
        assert c.get("/config.json").json()["collections"] == {"path": "collections"}
    assert (mine / "mine.json").is_file()
    assert not (drafts / "collections").exists()


def test_the_library_config_always_names_collections(tmp_path: Path) -> None:
    for collections_dir in (None, tmp_path / "mine"):
        s = Settings(drafts_dir=tmp_path / "drafts", collections_dir=collections_dir)
        assert library_config(s)["collections"] == {"path": "collections"}


def test_collection_list_answers_405_with_allow(client: TestClient) -> None:
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        r = client.request(method, "/collections")
        _is_error(r, 405, "error-method-not-allowed.json")
        assert r.headers["allow"] == "GET, HEAD"


# --- PUT / GET / DELETE /collections/{id} -----------------------------------------


def test_put_creates_then_replaces_and_returns_the_listed_doc(client: TestClient, personal: Path) -> None:
    r = _create(client, "mine", _collection("Mine", [SCREEN, TOOL], description="d"))
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-cache"
    doc = r.json()
    assert doc["source"] == "personal" and doc["ref"] == "personal:mine" and doc["id"] == "mine"
    assert doc["members"] == [SCREEN, TOOL] and doc["description"] == "d"
    stored = json.loads((personal / "mine.json").read_text())
    assert "source" not in stored and "ref" not in stored and stored["id"] == "mine"
    r = _put(client, "mine", _collection("Renamed"), _match(doc["rev"]))
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    got = client.get("/collections/mine")
    assert got.status_code == 200 and got.headers["cache-control"] == "no-cache"
    assert got.json() == r.json()


def test_put_with_default_is_400(client: TestClient, personal: Path) -> None:
    _is_error(_create(client, "mine", _collection(default=False)), 400, "error-invalid-collection.json")
    assert not (personal / "mine.json").exists()


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"name": "", "members": []},
        {"name": "x", "members": [], "extra": 1},
        {"name": "x", "members": [{"screen": "elsewhere/x"}]},
        {"name": "x", "members": [], "id": "other"},
    ],
)
def test_put_with_a_broken_rule_is_400_invalid_collection(client: TestClient, personal: Path, doc) -> None:
    r = _create(client, "mine", doc)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection" and r.json()["detail"]
    assert not (personal / "mine.json").exists()


def test_aggregate_size_over_the_limit_is_400_naming_the_total(client: TestClient) -> None:
    members = [{"tool": "trace", "query": "q" * 4000, "name": f"m{i}"} for i in range(MAX_COLLECTION_BYTES // 4000)]
    r = _create(client, "big", _collection("Big", members[:50], description="d" * (MAX_COLLECTION_BYTES - 50 * 4000)))
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection"
    assert "MAX_COLLECTION_BYTES" in r.json()["detail"]


def test_put_body_over_the_transport_cap_is_413(client: TestClient, personal: Path) -> None:
    body = json.dumps(_collection(description="d" * MAX_PUT_BODY_BYTES))
    _is_error(client.put("/collections/v", content=body, headers={**JSON, **CREATE}), 413, "error-too-large.json")

    def chunks():
        for _ in range(MAX_PUT_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    assert client.put("/collections/v", content=chunks(), headers={**JSON, **CREATE}).status_code == 413
    assert not (personal / "v.json").exists()


def test_put_needs_json_content_type(client: TestClient) -> None:
    r = client.put(
        "/collections/v", content=json.dumps(_collection()), headers={"content-type": "text/plain", **CREATE}
    )
    _is_error(r, 415, "error-unsupported-media-type.json")
    r = client.put(
        "/collections/v",
        content=json.dumps(_collection()),
        headers={"content-type": "Application/JSON; charset=utf-8", **CREATE},
    )
    assert r.status_code == 201


def test_put_bad_json_is_400(client: TestClient) -> None:
    _is_error(
        client.put("/collections/v", content=b"{nope", headers={**JSON, **CREATE}), 400, "error-invalid-json.json"
    )


def test_create_only_put_on_an_existing_id_is_412(client: TestClient, personal: Path) -> None:
    assert _put(client, "mine", _collection("First"), {"if-none-match": "*"}).status_code == 201
    _is_error(_put(client, "mine", _collection("Second"), {"if-none-match": "*"}), 412, "error-exists.json")
    assert json.loads((personal / "mine.json").read_text())["name"] == "First"


@pytest.mark.parametrize("headers", [{}, {"if-none-match": '"abc"'}, {"if-none-match": "W/*"}])
def test_put_without_a_precondition_on_an_existing_id_is_428_and_the_file_is_unchanged(
    client: TestClient, personal: Path, headers: dict
) -> None:
    assert _create(client, "mine", _collection("First")).status_code == 201
    before = (personal / "mine.json").read_bytes()
    body = _is_error(_put(client, "mine", _collection("Second"), headers), 428, "error-precondition-required.json")
    assert "If-None-Match" in body["detail"] and "If-Match" in body["detail"]
    assert (personal / "mine.json").read_bytes() == before


def test_put_without_a_precondition_on_a_new_id_is_428_and_stores_nothing(client: TestClient, personal: Path) -> None:
    _is_error(_put(client, "mine", _collection()), 428, "error-precondition-required.json")
    assert not (personal / "mine.json").exists()


@pytest.mark.parametrize("form", ["{rev}", '"{rev}"', 'W/"{rev}"', ' W/"{rev}" ', '"0000000000000000", "{rev}"'])
def test_if_match_with_the_current_rev_replaces(client: TestClient, personal: Path, form: str) -> None:
    first = _create(client, "mine", _collection("First")).json()
    r = _put(client, "mine", _collection("Second"), _match(form.format(rev=first["rev"])))
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["name"] == "Second" and second["rev"] != first["rev"]
    assert json.loads((personal / "mine.json").read_text())["name"] == "Second"
    # The answered rev is the stored one: the list and GET agree, and it replaces again.
    assert client.get("/collections/mine").json()["rev"] == second["rev"]
    assert _put(client, "mine", _collection("Third"), _match(second["rev"])).status_code == 200


def test_if_match_with_an_old_rev_is_412_stale_and_the_file_is_unchanged(client: TestClient, personal: Path) -> None:
    first = _create(client, "mine", _collection("First")).json()
    assert _put(client, "mine", _collection("Second"), _match(first["rev"])).status_code == 200
    before = (personal / "mine.json").read_bytes()
    _is_error(_put(client, "mine", _collection("Third"), _match(first["rev"])), 412, "error-stale.json")
    assert (personal / "mine.json").read_bytes() == before


def test_if_match_on_a_missing_id_is_412_gone_and_stores_nothing(client: TestClient, personal: Path) -> None:
    _is_error(_put(client, "mine", _collection(), _match("0123456789abcdef")), 412, "error-gone.json")
    assert not (personal / "mine.json").exists()
    # Deleted under the client: the rev it saw is gone too.
    rev = _create(client, "mine", _collection()).json()["rev"]
    assert client.delete("/collections/mine").status_code == 204
    _is_error(_put(client, "mine", _collection(), _match(rev)), 412, "error-gone.json")
    assert not (personal / "mine.json").exists()


def test_if_match_wins_over_if_none_match_on_put(client: TestClient, personal: Path) -> None:
    rev = _create(client, "mine", _collection("First")).json()["rev"]
    r = _put(client, "mine", _collection("Second"), {**CREATE, **_match(rev)})
    assert r.status_code == 200 and r.json()["name"] == "Second"


def test_delete_honours_if_match(client: TestClient, personal: Path) -> None:
    first = _create(client, "mine", _collection("First")).json()
    second = _put(client, "mine", _collection("Second"), _match(first["rev"])).json()
    _is_error(client.delete("/collections/mine", headers=_match(first["rev"])), 412, "error-stale.json")
    assert json.loads((personal / "mine.json").read_text())["name"] == "Second"
    r = client.delete("/collections/mine", headers=_match(f'W/"{second["rev"]}"'))
    assert r.status_code == 204 and r.headers["cache-control"] == "no-cache"
    assert not (personal / "mine.json").exists()


def test_conditional_delete_of_a_missing_id_is_404(client: TestClient) -> None:
    _is_error(client.delete("/collections/ghost", headers=_match("0123456789abcdef")), 404, "error-not-found.json")


def test_stale_delete_keeps_the_ref_ticked(client: TestClient) -> None:
    assert _create(client, "mine", _collection()).status_code == 201
    _op(client, {"op": "tick", "ref": "personal:mine"})
    assert client.delete("/collections/mine", headers=_match("0" * 16)).status_code == 412
    assert client.get("/collections-state").json()["ticked"] == ["personal:mine"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_bad_ids_are_400(client: TestClient, bad: str) -> None:
    for method in ("GET", "PUT", "DELETE"):
        r = client.request(method, f"/collections/{bad}", content=json.dumps(_collection()), headers=JSON)
        assert r.status_code == 400, (method, bad)
        assert r.json()["code"] == "invalid_id"
        assert r.headers["cache-control"] == "no-cache"


def test_bad_id_body_fixture(client: TestClient) -> None:
    _is_error(client.get("/collections/a/b"), 400, "error-invalid-id.json")


def test_the_state_file_is_never_addressable(client: TestClient, personal: Path) -> None:
    assert _op(client, {"op": "tick", "ref": "facility:x"}).status_code == 200
    assert (personal / STATE_FILE).is_file()
    assert client.get("/collections/.state").status_code == 400
    assert client.delete("/collections/.state").status_code == 400
    assert (personal / STATE_FILE).is_file()


def test_delete_and_not_found(client: TestClient, personal: Path) -> None:
    _is_error(client.get("/collections/ghost"), 404, "error-not-found.json")
    assert client.delete("/collections/ghost").status_code == 404
    assert _create(client, "mine", _collection()).status_code == 201
    r = client.delete("/collections/mine")
    assert r.status_code == 204
    assert r.headers["cache-control"] == "no-cache"
    assert not (personal / "mine.json").exists()
    assert client.get("/collections/mine").status_code == 404


def test_delete_prunes_the_personal_ref_from_both_lists(client: TestClient) -> None:
    for collection_id in ("keep", "gone"):
        assert _create(client, collection_id, _collection(collection_id)).status_code == 201
    _op(client, {"op": "tick", "ref": "personal:gone"})
    _op(client, {"op": "tick", "ref": "personal:keep"})
    _op(client, {"op": "untick", "ref": "facility:gone"})
    assert client.delete("/collections/gone").status_code == 204
    state = client.get("/collections-state").json()
    assert state["ticked"] == ["personal:keep"]
    assert state["unticked"] == ["facility:gone"]


def test_same_id_in_both_stores_lists_twice_and_delete_removes_personal_only(
    client: TestClient, facility: Path, personal: Path
) -> None:
    _write(facility, "shared.json", _collection("Shared"))
    assert _create(client, "shared", _collection("Shared", [DRAFT])).status_code == 201
    listed = client.get("/collections").json()["collections"]
    assert [c["ref"] for c in listed] == ["facility:shared", "personal:shared"]
    assert client.delete("/collections/shared").status_code == 204
    assert [c["ref"] for c in client.get("/collections").json()["collections"]] == ["facility:shared"]
    assert (facility / "shared.json").is_file()
    assert not (personal / "shared.json").exists()
    # The facility one is not personal: there is nothing left to delete.
    assert client.delete("/collections/shared").status_code == 404


def test_item_answers_405_with_allow(client: TestClient) -> None:
    for method in ("POST", "PATCH"):
        r = client.request(method, "/collections/mine")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD, PUT, DELETE"
        assert r.headers["cache-control"] == "no-cache"
        assert r.json()["code"] == "method_not_allowed"


# --- /collections-state ----------------------------------------------------------


def test_state_starts_empty(client: TestClient, personal: Path) -> None:
    r = client.get("/collections-state")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.json() == _fixture("state-empty.json")
    assert not personal.exists()


def test_post_returns_the_new_state_document(client: TestClient, facility: Path) -> None:
    _write(facility, "injector.json", _collection(default=True))
    _op(client, {"op": "untick", "ref": "facility:injector"})
    _op(client, {"op": "tick", "ref": "personal:mine"})
    _op(client, {"op": "pin", "member": TOOL})
    _op(client, {"op": "recent", "member": DRAFT})
    r = _op(client, {"op": "recent", "member": SCREEN})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.json() == _fixture("state.json")
    assert client.get("/collections-state").json() == r.json()


def test_ops_are_idempotent(client: TestClient) -> None:
    first = _op(client, {"op": "pin", "member": SCREEN}).json()
    assert _op(client, {"op": "pin", "member": SCREEN}).json() == first
    _op(client, {"op": "tick", "ref": "facility:x"})
    assert _op(client, {"op": "tick", "ref": "facility:x"}).json()["ticked"] == ["facility:x"]
    assert _op(client, {"op": "unpin", "member": SCREEN}).json()["pinned"] == []
    assert _op(client, {"op": "unpin", "member": SCREEN}).json()["pinned"] == []


def test_recent_dedups_moves_first_and_keeps_ten(client: TestClient) -> None:
    for i in range(MAX_RECENT + 2):
        _op(client, {"op": "recent", "member": {"tool": "trace", "query": f"pv=P{i}"}})
    state = _op(client, {"op": "recent", "member": {"tool": "trace", "query": "pv=P5"}}).json()
    queries = [m["query"] for m in state["recent"]]
    assert len(queries) == MAX_RECENT
    assert queries[0] == "pv=P5" and queries.count("pv=P5") == 1
    assert "pv=P0" not in queries and "pv=P1" not in queries


def test_recent_remove_and_recent_clear(client: TestClient) -> None:
    _op(client, {"op": "pin", "member": TOOL})
    _op(client, {"op": "recent", "member": SCREEN})
    _op(client, {"op": "recent", "member": DRAFT})
    r = _op(client, {"op": "recent-remove", "member": SCREEN})
    assert r.status_code == 200 and r.json()["recent"] == [DRAFT]
    r = _op(client, {"op": "recent-clear"})
    assert r.status_code == 200
    assert r.json()["recent"] == [] and r.json()["pinned"] == [TOOL]
    assert client.get("/collections-state").json() == r.json()


def test_recent_many_lands_members_first_in_order(client: TestClient) -> None:
    _op(client, {"op": "recent", "member": TOOL})
    r = _op(client, {"op": "recent-many", "members": [SCREEN, DRAFT, TOOL]})
    assert r.status_code == 200 and r.json()["recent"] == [SCREEN, DRAFT, TOOL]


def test_recent_clear_with_a_member_is_400_invalid_op(client: TestClient, personal: Path) -> None:
    r = _op(client, {"op": "recent-clear", "member": SCREEN})
    assert r.status_code == 400
    assert r.json() == {"code": "invalid_op", "detail": "a recent-clear op is exactly {op}"}
    assert not (personal / STATE_FILE).exists()


def test_recent_many_over_max_recent_is_400_invalid_op(client: TestClient, personal: Path) -> None:
    members = [{"tool": "trace", "query": f"pv=M{i}"} for i in range(MAX_RECENT + 1)]
    r = _op(client, {"op": "recent-many", "members": members})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_op" and "MAX_RECENT" in r.json()["detail"]
    assert not (personal / STATE_FILE).exists()


def test_recent_many_op_cap_is_48_kib(client: TestClient, personal: Path) -> None:
    assert MAX_RECENT_MANY_OP_BYTES == 48 * 1024 < MAX_POST_BODY_BYTES
    fits = [{"tool": "trace", "query": f"pv=B{i}&" + "q" * 4000} for i in range(MAX_RECENT)]
    assert _op(client, {"op": "recent-many", "members": fits}).status_code == 200
    big = [{"tool": "trace", "query": f"pv=C{i}&" + "q" * 5000} for i in range(MAX_RECENT)]
    r = _op(client, {"op": "recent-many", "members": big})
    assert r.status_code == 413
    assert r.json()["code"] == "too_large" and "MAX_RECENT_MANY_OP_BYTES" in r.json()["detail"]


def test_invalid_op_detail_lists_every_state_op(client: TestClient) -> None:
    r = _op(client, {"op": "bogus"})
    assert r.json() == {"code": "invalid_op", "detail": f"op must be one of {', '.join(STATE_OPS)}"}
    assert "recent-many" in r.json()["detail"]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "bogus"},
        {"op": "tick"},
        {"op": "tick", "ref": "nowhere:x"},
        {"op": "pin", "member": {**SCREEN, "slot": "a"}},
        {"op": "pin", "member": {"screen": "elsewhere/x"}},
        {"op": "recent", "member": SCREEN, "extra": 1},
        {"op": "recent-clear", "member": SCREEN},
        {"op": "recent-remove", "members": [SCREEN]},
        {"op": "recent-many", "members": [{**SCREEN, "slot": "a"}]},
        ["not", "an", "object"],
    ],
)
def test_bad_ops_are_400_invalid_op(client: TestClient, personal: Path, op) -> None:
    r = _op(client, op)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_op" and r.json()["detail"]
    assert not (personal / STATE_FILE).exists()


def test_invalid_op_body_fixture(client: TestClient) -> None:
    _is_error(_op(client, {"op": "bogus"}), 400, "error-invalid-op.json")


def test_op_over_its_limit_is_413(client: TestClient, personal: Path) -> None:
    # Within the transport cap but over MAX_STATE_OP_BYTES: the op's own 413.
    op = {"op": "recent", "member": {"tool": "trace", "query": "q" * 100, "name": "n" * MAX_STATE_OP_BYTES}}
    r = _op(client, op)
    assert r.status_code == 413
    assert r.json()["code"] == "too_large" and "MAX_STATE_OP_BYTES" in r.json()["detail"]
    # Over the transport cap.
    body = json.dumps({"op": "recent", "member": {"tool": "trace", "name": "n" * MAX_POST_BODY_BYTES}})
    r = client.post("/collections-state", content=body, headers=JSON)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert not (personal / STATE_FILE).exists()


def test_post_needs_json_and_valid_json(client: TestClient) -> None:
    r = client.post("/collections-state", content=b'{"op": "tick"}', headers={"content-type": "text/plain"})
    _is_error(r, 415, "error-unsupported-media-type.json")
    _is_error(client.post("/collections-state", content=b"{nope", headers=JSON), 400, "error-invalid-json.json")


def test_pin_at_the_cap_is_409_pinned_full(client: TestClient) -> None:
    for i in range(MAX_PINNED):
        assert _op(client, {"op": "pin", "member": {"tool": "trace", "query": f"pv=P{i}"}}).status_code == 200
    _is_error(_op(client, {"op": "pin", "member": SCREEN}), 409, "error-pinned-full.json")
    # Pinning one already pinned is still idempotent at the cap.
    assert _op(client, {"op": "pin", "member": {"tool": "trace", "query": "pv=P0"}}).status_code == 200


def test_tick_at_the_cap_evicts_unknown_refs_then_is_409_selection_full(client: TestClient, facility: Path) -> None:
    for i in range(MAX_REFS):
        _write(facility, f"c{i}.json", _collection(f"C{i}"))
    for i in range(MAX_REFS - 1):
        assert _op(client, {"op": "tick", "ref": f"facility:c{i}"}).status_code == 200
    _op(client, {"op": "tick", "ref": "personal:gone"})
    # Full of current refs but one: the unknown ref is evicted to make room.
    state = _op(client, {"op": "tick", "ref": f"facility:c{MAX_REFS - 1}"}).json()
    assert len(state["ticked"]) == MAX_REFS and "personal:gone" not in state["ticked"]
    _is_error(_op(client, {"op": "tick", "ref": "facility:new"}), 409, "error-selection-full.json")


def test_state_answers_405_with_allow(client: TestClient) -> None:
    for method in ("PUT", "DELETE", "PATCH"):
        r = client.request(method, "/collections-state")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD, POST"
        assert r.headers["cache-control"] == "no-cache"


# --- guards ----------------------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
def test_cross_site_writes_are_403(client: TestClient, personal: Path, site: str) -> None:
    assert _create(client, "keep", _collection("Keep")).status_code == 201
    headers = {"sec-fetch-site": site}
    _is_error(_put(client, "v", _collection(), headers), 403, "error-cross-site.json")
    assert not (personal / "v.json").exists()
    _is_error(client.delete("/collections/keep", headers=headers), 403, "error-cross-site.json")
    assert (personal / "keep.json").exists()
    _is_error(_op(client, {"op": "tick", "ref": "facility:x"}, headers), 403, "error-cross-site.json")
    assert not (personal / STATE_FILE).exists()
    # Reads are not guarded.
    assert client.get("/collections", headers=headers).status_code == 200
    assert client.get("/collections/keep", headers=headers).status_code == 200
    assert client.get("/collections-state", headers=headers).status_code == 200


@pytest.mark.parametrize("site", ["same-origin", "none", " Same-Origin "])
def test_same_origin_writes_pass(client: TestClient, site: str) -> None:
    headers = {"sec-fetch-site": site}
    assert _create(client, "v", _collection(), headers).status_code == 201
    assert _op(client, {"op": "tick", "ref": "personal:v"}, headers).status_code == 200
    assert client.delete("/collections/v", headers=headers).status_code == 204


def _refuse(code: int):
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    return refuse


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_unwritable_personal_dir_is_403_read_only_store(
    client: TestClient, personal: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    keep = _create(client, "keep", _collection("Keep")).json()
    monkeypatch.setattr("canopy_mcp.store.os.replace", _refuse(code))
    for collection_id, headers in (("keep", _match(keep["rev"])), ("new", CREATE)):
        body = _is_error(_put(client, collection_id, _collection("n"), headers), 403, "error-read-only-store.json")
        assert str(personal) not in json.dumps(body)
    _is_error(_op(client, {"op": "pin", "member": SCREEN}), 403, "error-read-only-store.json")
    assert json.loads((personal / "keep.json").read_text())["name"] == "Keep"
    # Reads still answer.
    assert client.get("/collections").status_code == 200
    assert client.get("/collections-state").status_code == 200


def test_unwritable_delete_is_403_read_only_store(
    client: TestClient, personal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _create(client, "keep", _collection("Keep")).status_code == 201
    monkeypatch.setattr("canopy_mcp.store.os.unlink", _refuse(errno.EROFS))
    monkeypatch.setattr("canopy_mcp.store.os.remove", _refuse(errno.EROFS))
    _is_error(client.delete("/collections/keep"), 403, "error-read-only-store.json")
    assert (personal / "keep.json").exists()


# --- concurrency -----------------------------------------------------------------


def _parallel(calls) -> list:
    results: list = [None] * len(calls)
    start = threading.Barrier(len(calls))

    def run(i, call):
        start.wait()
        results[i] = call()

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_parallel_recent_and_pin_posts_lose_nothing(client: TestClient) -> None:
    recents = [{"tool": "trace", "query": f"pv=R{i}"} for i in range(MAX_RECENT)]
    pins = [{"tool": "correlation", "query": f"pv=P{i}"} for i in range(12)]
    calls = [lambda m=m: _op(client, {"op": "recent", "member": m}) for m in recents]
    calls += [lambda m=m: _op(client, {"op": "pin", "member": m}) for m in pins]
    assert all(r.status_code == 200 for r in _parallel(calls))
    state = client.get("/collections-state").json()
    assert sorted(m["query"] for m in state["recent"]) == sorted(m["query"] for m in recents)
    assert sorted(m["query"] for m in state["pinned"]) == sorted(m["query"] for m in pins)


def test_parallel_posts_from_two_processes_lose_nothing(settings: Settings) -> None:
    # Two app instances over one personal dir stand in for two processes: only the flock orders them.
    with (
        TestClient(create_app(settings, startup_hooks=[])) as one,
        TestClient(create_app(settings, startup_hooks=[])) as two,
    ):
        calls = []
        for i in range(8):
            c = one if i % 2 else two
            calls.append(lambda c=c, i=i: _op(c, {"op": "pin", "member": {"tool": "trace", "query": f"pv=P{i}"}}))
            calls.append(lambda c=c, i=i: _op(c, {"op": "recent", "member": {"tool": "waveform", "query": f"pv=W{i}"}}))
        assert all(r.status_code == 200 for r in _parallel(calls))
        state = one.get("/collections-state").json()
    assert sorted(m["query"] for m in state["pinned"]) == [f"pv=P{i}" for i in range(8)]
    assert sorted(m["query"] for m in state["recent"]) == [f"pv=W{i}" for i in range(8)]


def test_limits_sit_above_the_document_limits() -> None:
    assert MAX_PUT_BODY_BYTES > MAX_COLLECTION_BYTES
    assert MAX_POST_BODY_BYTES > MAX_STATE_OP_BYTES


# --- fixtures --------------------------------------------------------------------


def test_every_response_fixture_is_an_error_body_or_a_state_document() -> None:
    names = sorted(path.name for path in RESPONSES.glob("*.json"))
    assert names, "no response fixtures"
    for name in names:
        body = _fixture(name)
        if name.startswith("state"):
            assert set(body) == {"ticked", "unticked", "pinned", "recent"}
        elif name.startswith("list"):
            assert set(body) == {"collections"}
        else:
            assert name.startswith("error-")
            assert set(body) <= {"code", "detail"} and body["code"] == name[len("error-") : -5].replace("-", "_")


# --- conditional GET (ETag) on the list and the state ---------------------------


def _sha_etag(body: bytes) -> str:
    import hashlib

    return f'"{hashlib.sha256(body).hexdigest()}"'


@pytest.mark.parametrize("path", ["/collections", "/collections-state"])
def test_list_and_state_carry_an_etag_of_the_body(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["etag"] == _sha_etag(r.content)
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/collections", "/collections-state"])
def test_unchanged_etag_is_304_with_no_body(client: TestClient, path: str) -> None:
    assert _create(client, "mine", _collection()).status_code == 201
    first = client.get(path)
    r = client.get(path, headers={"if-none-match": first.headers["etag"]})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == first.headers["etag"]
    assert r.headers["cache-control"] == "no-cache"
    # Weak and list forms match too.
    listed = client.get(path, headers={"if-none-match": f'"other", W/{first.headers["etag"]}'})
    assert listed.status_code == 304


def test_changed_list_etag_is_200_with_a_new_etag(client: TestClient) -> None:
    first = client.get("/collections")
    assert _create(client, "mine", _collection()).status_code == 201
    r = client.get("/collections", headers={"if-none-match": first.headers["etag"]})
    assert r.status_code == 200
    assert r.headers["etag"] != first.headers["etag"]
    assert r.headers["etag"] == _sha_etag(r.content)
    assert [c["id"] for c in r.json()["collections"]] == ["mine"]


def test_changed_state_etag_is_200_with_a_new_etag(client: TestClient) -> None:
    assert _create(client, "mine", _collection()).status_code == 201
    first = client.get("/collections-state")
    assert _op(client, {"op": "tick", "ref": "personal:mine"}).status_code == 200
    r = client.get("/collections-state", headers={"if-none-match": first.headers["etag"]})
    assert r.status_code == 200
    assert r.headers["etag"] != first.headers["etag"]
    assert r.headers["etag"] == _sha_etag(r.content)
    assert r.json()["ticked"] == ["personal:mine"]


@pytest.mark.parametrize("path", ["/collections", "/collections-state"])
def test_head_mirrors_get_etag(client: TestClient, path: str) -> None:
    got = client.get(path)
    head = client.head(path)
    assert head.status_code == 200
    assert head.headers["etag"] == got.headers["etag"]
    assert head.headers["cache-control"] == "no-cache"
    cached = client.head(path, headers={"if-none-match": got.headers["etag"]})
    assert cached.status_code == 304
    assert cached.headers["etag"] == got.headers["etag"]


def test_mismatched_etag_is_200_with_the_body(client: TestClient) -> None:
    r = client.get("/collections", headers={"if-none-match": '"nope"'})
    assert r.status_code == 200
    assert r.headers["etag"] == _sha_etag(r.content)
    assert r.json() == {"collections": []}


# --- a member with a slot --------------------------------------------------------


def test_a_stored_file_with_a_slot_member_is_skipped_and_never_opened(client: TestClient, personal: Path) -> None:
    _write(personal, "slotted.json", {"name": "Slotted", "members": [{**SCREEN, "slot": "a"}]})
    assert client.get("/collections").json()["collections"] == []
    r = client.get("/collections/slotted")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_a_put_with_a_slot_member_is_400(client: TestClient) -> None:
    r = _create(client, "slotted", {"name": "Slotted", "members": [{**SCREEN, "slot": "a"}]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_collection"
    assert "unknown key(s): slot" in r.json()["detail"]


@pytest.mark.parametrize(
    "layout, reason",
    [
        ({"tabs": [0]}, "member_missing"),
        ({"tabs": [0, 1], "showing": 5}, "showing_not_in_tabs"),
        ({"split": "row", "items": [{"tabs": [0, 1]}]}, "split_too_few_items"),
    ],
)
def test_a_put_with_a_broken_layout_is_400_naming_the_reason(client: TestClient, layout, reason) -> None:
    r = _create(client, "mine", _collection(members=[SCREEN, TOOL], layout=layout))
    assert r.status_code == 400 and f": {reason}: " in r.json()["detail"]
