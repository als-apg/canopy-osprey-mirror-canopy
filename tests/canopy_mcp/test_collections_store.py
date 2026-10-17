"""The collection stores and the per-user state document (canopy_mcp.collections)."""

import os
import json
import hashlib
import asyncio
import logging

import pytest

from canopy_mcp.config import Settings
from canopy_mcp.member_key import member_key
from canopy_mcp.collections import (
    OP_FIELDS,
    STATE_OPS,
    MAX_REFS,
    MAX_PINNED,
    MAX_RECENT,
    STATE_FILE,
    MAX_STATE_BYTES,
    MAX_STATE_OP_BYTES,
    MAX_RECENT_MANY_OP_BYTES,
    MAX_COLLECTION_BYTES,
    PinnedFull,
    ReadOnlyStore,
    SelectionFull,
    StateTooLarge,
    InvalidStateOp,
    CollectionStore,
    CollectionExists,
    InvalidCollection,
    CollectionNotFound,
    FacilityCollections,
    InvalidCollectionId,
    _size,
    empty_state,
    all_collections,
)

SCREEN = {"screen": "library/sr/overview"}
TOOL = {"tool": "trace", "query": "pv=A:B&range=-1h", "name": "Beam current"}


def _write(directory, name, doc):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def _collection(name="Injector", members=None, **extra):
    return {"name": name, "members": [SCREEN] if members is None else members, **extra}


@pytest.fixture
def facility_dir(tmp_path):
    return tmp_path / "facility"


@pytest.fixture
def personal_dir(tmp_path):
    return tmp_path / "personal"


@pytest.fixture
def store(personal_dir, facility_dir):
    return CollectionStore(personal_dir, facility=FacilityCollections(facility_dir))


# --- settings ---------------------------------------------------------------------


def test_settings_default_personal_dir_under_drafts(monkeypatch, tmp_path):
    for name in ("CANOPY_COLLECTIONS_DIR", "CANOPY_FACILITY_COLLECTIONS_DIR"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(drafts_dir=tmp_path / "drafts")
    assert s.collections_dir is None
    assert s.facility_collections_dir is None
    assert s.personal_collections_dir == tmp_path / "drafts" / "collections"


def test_settings_read_both_dirs_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_COLLECTIONS_DIR", str(tmp_path / "mine"))
    monkeypatch.setenv("CANOPY_FACILITY_COLLECTIONS_DIR", str(tmp_path / "facility"))
    s = Settings()
    assert s.personal_collections_dir == tmp_path / "mine"
    assert s.facility_collections_dir == tmp_path / "facility"


def test_settings_empty_dirs_count_as_unset(monkeypatch):
    monkeypatch.setenv("CANOPY_COLLECTIONS_DIR", "")
    monkeypatch.setenv("CANOPY_FACILITY_COLLECTIONS_DIR", "")
    s = Settings()
    assert s.collections_dir is None
    assert s.facility_collections_dir is None


# --- facility reader --------------------------------------------------------------


def test_facility_id_is_the_file_stem_and_updated_is_optional(facility_dir):
    path = _write(facility_dir, "injector.json", _collection(default=True))
    (doc,) = FacilityCollections(facility_dir).list()
    assert doc["id"] == "injector"
    assert doc["source"] == "facility"
    assert doc["ref"] == "facility:injector"
    assert doc["default"] is True
    assert doc["updated"].endswith("Z")
    os.utime(path, (0, 0))
    (doc,) = FacilityCollections(facility_dir).list()
    assert doc["updated"] == "1970-01-01T00:00:00.000Z"


def test_facility_keeps_a_stated_updated_and_a_matching_id(facility_dir):
    _write(facility_dir, "a.json", _collection(id="a", updated="2026-10-01T00:00:00Z"))
    (doc,) = FacilityCollections(facility_dir).list()
    assert doc["updated"] == "2026-10-01T00:00:00Z"


def test_facility_sorted_by_casefolded_name_then_id(facility_dir):
    _write(facility_dir, "b.json", _collection("beta"))
    _write(facility_dir, "a2.json", _collection("Alpha"))
    _write(facility_dir, "a1.json", _collection("alpha"))
    assert [doc["id"] for doc in FacilityCollections(facility_dir).list()] == ["a1", "a2", "b"]


def test_facility_missing_dir_lists_nothing(facility_dir):
    assert FacilityCollections(facility_dir).list() == []


def test_facility_bad_file_is_logged_once_and_skipped(facility_dir, caplog):
    _write(facility_dir, "good.json", _collection())
    bad = _write(facility_dir, "bad.json", "{not json")
    _write(facility_dir, "mismatch.json", _collection(id="other"))
    _write(facility_dir, "drafty.json", _collection(members=[{"screen": "drafts/x"}]))
    _write(facility_dir, "bad id.json", _collection())
    reader = FacilityCollections(facility_dir)
    with caplog.at_level(logging.WARNING, logger="canopy_mcp.collections"):
        assert [doc["id"] for doc in reader.list()] == ["good"]
        first = len(caplog.records)
        assert first == 4
        assert [doc["id"] for doc in reader.list()] == ["good"]
        assert len(caplog.records) == first
        bad.write_text("{still not json, but longer", encoding="utf-8")
        reader.list()
        assert len(caplog.records) == first + 1


# --- personal store ---------------------------------------------------------------


async def test_put_creates_then_replaces(store, personal_dir):
    doc, created = await store.put("mine", _collection("  Mine  "))
    assert created is True
    assert doc["id"] == "mine" and doc["name"] == "Mine"
    assert doc["source"] == "personal" and doc["ref"] == "personal:mine"
    assert doc["updated"].endswith("Z")
    stored = json.loads((personal_dir / "mine.json").read_text())
    assert stored["id"] == "mine" and "source" not in stored and "ref" not in stored
    doc, created = await store.put("mine", _collection("Renamed", updated="ignored"))
    assert created is False
    assert doc["updated"] != "ignored"
    assert store.get("mine")["name"] == "Renamed"


def _rev(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def test_facility_rev_is_the_sha256_prefix_of_the_file_bytes(facility_dir):
    path = _write(facility_dir, "a.json", _collection())
    (doc,) = FacilityCollections(facility_dir).list()
    assert doc["rev"] == _rev(path.read_bytes())
    assert len(doc["rev"]) == 16 and int(doc["rev"], 16) >= 0
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    (changed,) = FacilityCollections(facility_dir).list()
    assert changed["rev"] == _rev(path.read_bytes()) != doc["rev"]


async def test_put_returns_the_rev_of_the_bytes_it_wrote(store, personal_dir):
    doc, _ = await store.put("mine", _collection())
    data = (personal_dir / "mine.json").read_bytes()
    assert doc["rev"] == _rev(data)
    assert store.get("mine")["rev"] == doc["rev"]
    (listed,) = store.list()
    assert listed["rev"] == doc["rev"]
    again, _ = await store.put("mine", _collection("Renamed"))
    assert again["rev"] != doc["rev"]
    assert again["rev"] == _rev((personal_dir / "mine.json").read_bytes())


async def test_an_echoed_rev_never_reaches_the_file(store, personal_dir):
    first, _ = await store.put("mine", _collection())
    echoed = {**first, "rev": "ffffffffffffffff"}  # the listed doc sent straight back
    doc, created = await store.put("mine", echoed)
    assert created is False
    stored = json.loads((personal_dir / "mine.json").read_text())
    assert not {"rev", "source", "ref"} & stored.keys()
    assert doc["rev"] == _rev((personal_dir / "mine.json").read_bytes())
    assert doc["source"] == "personal" and doc["ref"] == "personal:mine"


async def test_put_accepts_drafts_members_and_rejects_default(store):
    await store.put("d", _collection(members=[{"screen": "drafts/x"}, TOOL]))
    with pytest.raises(InvalidCollection, match="default"):
        await store.put("e", _collection(default=True))


async def test_put_rejects_an_id_field_other_than_the_url_id(store):
    with pytest.raises(InvalidCollection, match="file id"):
        await store.put("a", _collection(id="b"))


async def test_put_rejects_an_invalid_id(store):
    for bad in (".state", "a/b", "", "x" * 65):
        with pytest.raises(InvalidCollectionId):
            await store.put(bad, _collection())


async def test_create_only_on_an_existing_id_raises_exists(store):
    _, created = await store.put("x", _collection(), create_only=True)
    assert created
    with pytest.raises(CollectionExists):
        await store.put("x", _collection("Other"), create_only=True)
    assert store.get("x")["name"] == "Injector"


async def test_put_over_the_aggregate_cap_names_the_total(store):
    members = [{"tool": "trace", "query": "q" * 4000 + str(i), "name": "n"} for i in range(50)]
    doc = _collection(members=members, description="d" * 80_000)
    with pytest.raises(InvalidCollection, match=rf"the collection is \d+ bytes, at most {MAX_COLLECTION_BYTES}"):
        await store.put("big", doc)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_read_only_dir_raises_read_only_store(store, personal_dir):
    personal_dir.mkdir()
    personal_dir.chmod(0o555)
    try:
        with pytest.raises(ReadOnlyStore) as exc:
            await store.put("x", _collection())
        assert exc.value.code == "read_only_store"
        with pytest.raises(ReadOnlyStore):
            await store.apply_op({"op": "pin", "member": SCREEN})
    finally:
        personal_dir.chmod(0o755)


async def test_state_file_is_never_a_collection(store, personal_dir):
    await store.put("x", _collection())
    await store.apply_op({"op": "tick", "ref": "personal:x"})
    assert (personal_dir / STATE_FILE).is_file()
    assert [doc["id"] for doc in store.list()] == ["x"]
    with pytest.raises(InvalidCollectionId):
        store.get(".state")


async def test_get_missing_raises_not_found(store):
    with pytest.raises(CollectionNotFound):
        store.get("nope")


async def test_delete_prunes_the_ref_from_both_lists(store):
    await store.put("a", _collection())
    await store.put("b", _collection())
    await store.apply_op({"op": "tick", "ref": "personal:a"})
    await store.apply_op({"op": "untick", "ref": "personal:b"})
    await store.apply_op({"op": "tick", "ref": "facility:a"})
    await store.delete("a")
    await store.delete("b")
    assert store.list() == []
    state = store.state()
    assert state["ticked"] == ["facility:a"]
    assert state["unticked"] == []


async def test_delete_missing_raises_not_found(store):
    with pytest.raises(CollectionNotFound):
        await store.delete("nope")


async def test_same_id_in_both_stores_lists_twice(store, facility_dir):
    _write(facility_dir, "x.json", _collection("Facility X"))
    await store.put("x", _collection("Personal X"))
    listed = all_collections(store.facility, store)
    assert [doc["ref"] for doc in listed] == ["facility:x", "personal:x"]
    await store.delete("x")
    assert [doc["ref"] for doc in all_collections(store.facility, store)] == ["facility:x"]


# --- state ------------------------------------------------------------------------


def test_state_without_a_dir_is_empty_and_creates_nothing(store, personal_dir):
    assert store.state() == empty_state() == {"ticked": [], "unticked": [], "pinned": [], "recent": []}
    assert not personal_dir.exists()


async def test_tick_and_untick_move_a_ref_between_lists_idempotently(store):
    state = await store.apply_op({"op": "tick", "ref": "facility:a"})
    assert state["ticked"] == ["facility:a"] and state["unticked"] == []
    assert (await store.apply_op({"op": "tick", "ref": "facility:a"}))["ticked"] == ["facility:a"]
    state = await store.apply_op({"op": "untick", "ref": "facility:a"})
    assert state["ticked"] == [] and state["unticked"] == ["facility:a"]
    assert await store.apply_op({"op": "untick", "ref": "facility:a"}) == state
    assert store.state() == state


async def test_full_ref_list_evicts_refs_naming_no_collection(store, facility_dir):
    _write(facility_dir, "known.json", _collection())
    await store.apply_op({"op": "tick", "ref": "facility:known"})
    for i in range(MAX_REFS - 1):
        await store.apply_op({"op": "tick", "ref": f"facility:gone{i}"})
    assert len(store.state()["ticked"]) == MAX_REFS
    state = await store.apply_op({"op": "tick", "ref": "facility:new"})
    assert state["ticked"] == ["facility:known", "facility:new"]


async def test_full_ref_list_of_current_collections_raises_selection_full(store, facility_dir):
    for i in range(MAX_REFS):
        _write(facility_dir, f"c{i}.json", _collection(f"C{i}"))
        await store.apply_op({"op": "untick", "ref": f"facility:c{i}"})
    with pytest.raises(SelectionFull) as exc:
        await store.apply_op({"op": "untick", "ref": "facility:one-more"})
    assert exc.value.code == "selection_full"
    assert len(store.state()["unticked"]) == MAX_REFS
    # A ref already in the list is still a no-op, not a 409.
    await store.apply_op({"op": "untick", "ref": "facility:c0"})


async def test_pin_is_an_idempotent_set_op_capped_at_max_pinned(store):
    state = await store.apply_op({"op": "pin", "member": SCREEN})
    assert state["pinned"] == [SCREEN]
    renamed = {**TOOL, "name": "Other label"}
    await store.apply_op({"op": "pin", "member": TOOL})
    state = await store.apply_op({"op": "pin", "member": renamed})
    assert state["pinned"] == [SCREEN, TOOL]
    state = await store.apply_op({"op": "unpin", "member": renamed})
    assert state["pinned"] == [SCREEN]
    assert (await store.apply_op({"op": "unpin", "member": TOOL}))["pinned"] == [SCREEN]
    for i in range(MAX_PINNED - 1):
        await store.apply_op({"op": "pin", "member": {"tool": "trace", "query": f"pv=P{i}"}})
    with pytest.raises(PinnedFull) as exc:
        await store.apply_op({"op": "pin", "member": {"tool": "waveform"}})
    assert exc.value.code == "pinned_full"
    # Pinning something already pinned at the cap is still a no-op.
    assert len((await store.apply_op({"op": "pin", "member": SCREEN}))["pinned"]) == MAX_PINNED


async def test_recent_dedups_by_member_key_puts_first_and_truncates(store):
    a = {"screen": "library/a", "macros": {"x": "1", "y": "2"}}
    a_same = {"screen": "library/a", "macros": {"y": "2", "x": "1", "theme": "dark"}}
    assert member_key(a) == member_key(a_same)
    await store.apply_op({"op": "recent", "member": a})
    await store.apply_op({"op": "recent", "member": TOOL})
    state = await store.apply_op({"op": "recent", "member": a_same})
    assert state["recent"] == [a_same, TOOL]
    for i in range(MAX_RECENT + 3):
        state = await store.apply_op({"op": "recent", "member": {"tool": "trace", "query": f"pv=R{i}"}})
    assert len(state["recent"]) == MAX_RECENT
    assert state["recent"][0] == {"tool": "trace", "query": f"pv=R{MAX_RECENT + 2}"}


def test_state_ops_derive_from_the_op_fields_table():
    assert STATE_OPS == tuple(OP_FIELDS)
    assert OP_FIELDS == {
        "tick": "ref",
        "untick": "ref",
        "pin": "member",
        "unpin": "member",
        "recent": "member",
        "recent-remove": "member",
        "recent-clear": None,
        "recent-many": "members",
    }


async def test_recent_remove_drops_by_member_key_and_is_idempotent(store):
    a = {"screen": "library/a", "macros": {"x": "1"}}
    await store.apply_op({"op": "recent", "member": a})
    await store.apply_op({"op": "recent", "member": TOOL})
    state = await store.apply_op({"op": "recent-remove", "member": {**TOOL, "name": "Other label"}})
    assert state["recent"] == [a]
    assert (await store.apply_op({"op": "recent-remove", "member": TOOL}))["recent"] == [a]
    assert (await store.apply_op({"op": "recent-remove", "member": a}))["recent"] == []


async def test_recent_clear_empties_recent_only(store):
    await store.apply_op({"op": "pin", "member": SCREEN})
    await store.apply_op({"op": "recent", "member": TOOL})
    state = await store.apply_op({"op": "recent-clear"})
    assert state["recent"] == [] and state["pinned"] == [SCREEN]
    assert (await store.apply_op({"op": "recent-clear"}))["recent"] == []


async def test_recent_many_puts_members_first_in_order_and_truncates(store):
    for i in range(MAX_RECENT):
        await store.apply_op({"op": "recent", "member": {"tool": "trace", "query": f"pv=Old{i}"}})
    many = [{"tool": "trace", "query": f"pv=New{i}"} for i in range(3)]
    # An already-recent member moves up into the batch's position.
    old = {"tool": "trace", "query": "pv=Old0"}
    state = await store.apply_op({"op": "recent-many", "members": [*many, old]})
    assert state["recent"][:4] == [*many, old]
    assert len(state["recent"]) == MAX_RECENT
    assert [m["query"] for m in state["recent"]].count("pv=Old0") == 1
    full = [{"tool": "trace", "query": f"pv=F{i}"} for i in range(MAX_RECENT)]
    state = await store.apply_op({"op": "recent-many", "members": full})
    assert state["recent"] == full


async def test_recent_many_keeps_the_first_of_duplicate_keys(store):
    state = await store.apply_op({"op": "recent-many", "members": [TOOL, SCREEN, {**TOOL, "name": "Again"}]})
    assert state["recent"] == [TOOL, SCREEN]
    assert (await store.apply_op({"op": "recent-many", "members": []}))["recent"] == [TOOL, SCREEN]


async def test_recent_many_over_max_recent_is_refused_without_writing(store, personal_dir):
    members = [{"tool": "trace", "query": f"pv=M{i}"} for i in range(MAX_RECENT + 1)]
    with pytest.raises(InvalidStateOp, match="MAX_RECENT"):
        await store.apply_op({"op": "recent-many", "members": members})
    assert not (personal_dir / STATE_FILE).exists()


async def test_recent_many_names_the_bad_member_and_applies_nothing(store):
    await store.apply_op({"op": "recent", "member": SCREEN})
    with pytest.raises(InvalidStateOp, match=r"members\[1\]"):
        await store.apply_op({"op": "recent-many", "members": [TOOL, {"screen": "library/a", "slot": "s"}]})
    assert store.state()["recent"] == [SCREEN]


async def test_recent_many_has_its_own_op_cap(store):
    # Over MAX_STATE_OP_BYTES but within MAX_RECENT_MANY_OP_BYTES: accepted.
    members = [{"tool": "trace", "query": f"pv=B{i}&" + "q" * 4000} for i in range(MAX_RECENT)]
    assert _size({"op": "recent-many", "members": members}) > MAX_STATE_OP_BYTES
    state = await store.apply_op({"op": "recent-many", "members": members})
    assert state["recent"] == members
    big = [{"tool": "trace", "query": f"pv=C{i}&" + "q" * 5000} for i in range(MAX_RECENT)]
    with pytest.raises(StateTooLarge, match="MAX_RECENT_MANY_OP_BYTES"):
        await store.apply_op({"op": "recent-many", "members": big})
    assert _size({"op": "recent-many", "members": big}) > MAX_RECENT_MANY_OP_BYTES
    # The larger cap is for recent-many only.
    with pytest.raises(StateTooLarge, match="MAX_STATE_OP_BYTES"):
        await store.apply_op({"op": "recent", "member": {"tool": "trace", "name": "n" * MAX_STATE_OP_BYTES}})


async def test_state_members_follow_personal_rules(store):
    await store.apply_op({"op": "pin", "member": {"screen": "drafts/mine"}})
    state = await store.apply_op({"op": "pin", "member": {"tool": "trace", "name": "  Padded  "}})
    assert state["pinned"][-1] == {"tool": "trace", "name": "Padded"}


@pytest.mark.parametrize(
    "op",
    [
        [],
        {"op": "explode", "ref": "facility:a"},
        {"ref": "facility:a"},
        {"op": "tick"},
        {"op": "tick", "ref": "facility:a", "extra": 1},
        {"op": "tick", "ref": "elsewhere:a"},
        {"op": "tick", "ref": "facility:a/b"},
        {"op": "tick", "ref": "facility"},
        {"op": "tick", "member": SCREEN},
        {"op": "pin", "ref": "facility:a"},
        {"op": "pin", "member": {"screen": "library/a", "slot": "s1"}},
        {"op": "pin", "member": {"screen": "elsewhere/a"}},
        {"op": "recent", "member": {"tool": "nope"}},
        {"op": "unpin", "member": {"screen": "library/a", "tool": "trace"}},
        {"op": "pin", "member": "library/a"},
        {"op": "recent-clear", "member": SCREEN},
        {"op": "recent-remove"},
        {"op": "recent-remove", "ref": "facility:a"},
        {"op": "recent-many", "member": SCREEN},
        {"op": "recent-many", "members": SCREEN},
        {"op": "recent-many", "members": [SCREEN], "extra": 1},
        {"op": "recent-many", "members": [{"screen": "elsewhere/a"}]},
        {"op": ["tick"], "ref": "facility:a"},
    ],
)
async def test_bad_ops_are_refused(store, op):
    with pytest.raises(InvalidStateOp):
        await store.apply_op(op)


async def test_an_op_over_the_cap_is_too_large(store):
    member = {"screen": "library/a", "macros": {"m": "v" * MAX_STATE_OP_BYTES}}
    with pytest.raises(StateTooLarge, match=str(MAX_STATE_OP_BYTES)):
        await store.apply_op({"op": "pin", "member": member})


async def test_malformed_state_entries_are_dropped_on_read(store, personal_dir):
    _write(
        personal_dir,
        STATE_FILE,
        {"ticked": ["facility:a", "bogus", "facility:a"], "unticked": "nope", "pinned": [SCREEN, {"x": 1}], "z": 1},
    )
    assert store.state() == {"ticked": ["facility:a"], "unticked": [], "pinned": [SCREEN], "recent": []}
    _write(personal_dir, STATE_FILE, "{broken")
    assert store.state() == empty_state()
    state = await store.apply_op({"op": "tick", "ref": "personal:x"})
    assert state["ticked"] == ["personal:x"]


async def test_parallel_ops_from_two_stores_lose_nothing(personal_dir):
    one, two = CollectionStore(personal_dir), CollectionStore(personal_dir)
    ops = [
        (one if i % 2 else two).apply_op({"op": "pin", "member": {"tool": "trace", "query": f"pv=P{i}"}})
        for i in range(20)
    ]
    await asyncio.gather(*ops)
    assert len(one.state()["pinned"]) == 20


# --- the maximal legal state -----------------------------------------------------


def _max_member(tag: str) -> dict:
    """A distinct screen member whose ``pin`` op is as close to MAX_STATE_OP_BYTES as it gets."""
    member = {"screen": f"library/{tag}", "macros": {"m": ""}}
    lo, hi = 0, MAX_STATE_OP_BYTES
    while lo < hi:
        mid = (lo + hi + 1) // 2
        member["macros"]["m"] = "é" * (mid // 2) + "x" * (mid % 2)
        if _size({"op": "recent", "member": member}) <= MAX_STATE_OP_BYTES:
            lo = mid
        else:
            hi = mid - 1
    member["macros"]["m"] = "é" * (lo // 2) + "x" * (lo % 2)
    assert MAX_STATE_OP_BYTES - 2 <= _size({"op": "recent", "member": member}) <= MAX_STATE_OP_BYTES
    return member


async def test_the_maximal_legal_state_fits(store, personal_dir):
    pad = "z" * 62  # 2 digits + 62 = a 64-character id, the longest legal one
    for i in range(MAX_REFS):
        await store.apply_op({"op": "tick", "ref": f"personal:{i:02d}{pad}"})
        await store.apply_op({"op": "untick", "ref": f"facility:{i:02d}{pad}"})
    for i in range(MAX_PINNED):
        await store.apply_op({"op": "pin", "member": _max_member(f"p{i}")})
    for i in range(MAX_RECENT):
        await store.apply_op({"op": "recent", "member": _max_member(f"r{i}")})
    state = store.state()
    assert [len(state[name]) for name in ("ticked", "unticked", "pinned", "recent")] == [
        MAX_REFS,
        MAX_REFS,
        MAX_PINNED,
        MAX_RECENT,
    ]
    assert all(len(ref) == 73 for ref in state["ticked"] + state["unticked"])
    size = (personal_dir / STATE_FILE).stat().st_size
    assert size <= MAX_STATE_BYTES
    assert size > MAX_STATE_BYTES * 0.9
