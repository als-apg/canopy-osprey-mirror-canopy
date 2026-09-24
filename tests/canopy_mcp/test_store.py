"""The draft store: id rules, compare-and-replace writes, revs, the manifest and the static path guard."""

import os
import json
import asyncio

import pytest

from canopy_mcp.store import DraftStore, RevConflict, DraftNotFound, InvalidDraftId


def _doc(title: str = "Motor", **extra) -> dict:
    return {"schemaVersion": "1.0", "kind": "screen", "id": "x", "metadata": {"title": title}, **extra}


@pytest.fixture
def store(tmp_path) -> DraftStore:
    return DraftStore(tmp_path / "drafts")


# --- ids and paths ----------------------------------------------------------


@pytest.mark.parametrize("draft_id", ["a", "A_b-9", "a/b", "sub/dir/screen_1", "-", "_x"])
def test_validate_id_accepts_fr1_ids(draft_id):
    assert DraftStore.validate_id(draft_id) == draft_id


@pytest.mark.parametrize(
    "draft_id",
    ["", "/a", "a/", "a//b", "../a", "a/../b", "a/./b", ".", "a.b", "a b", "a\\b", "a\n", "é", "a/b.screen.json"],
)
def test_validate_id_rejects_everything_else(draft_id):
    with pytest.raises(InvalidDraftId):
        DraftStore.validate_id(draft_id)


def test_validate_id_rejects_non_strings():
    with pytest.raises(InvalidDraftId):
        DraftStore.validate_id(None)  # type: ignore[arg-type]


def test_path_for_nests_under_screens(store):
    assert store.path_for("a/b") == store.root / "screens" / "a" / "b.screen.json"
    assert store.path_for("top") == store.root / "screens" / "top.screen.json"


def test_path_for_refuses_invalid_id(store):
    with pytest.raises(InvalidDraftId):
        store.path_for("../escape")


# --- write / read / rev -----------------------------------------------------


async def test_create_then_read_round_trips(store):
    rev = await store.write("a/b", _doc(), None)
    doc, read_rev = store.read("a/b")
    assert doc == _doc()
    assert read_rev == rev
    assert store.rev("a/b") == rev
    assert len(rev) == 64 and int(rev, 16) >= 0


async def test_rev_is_sha256_of_file_bytes(store):
    import hashlib

    rev = await store.write("a", _doc(), None)
    assert rev == hashlib.sha256(store.path_for("a").read_bytes()).hexdigest()


async def test_serialization_is_byte_deterministic(store):
    rev1 = await store.write("a", _doc(), None)
    rev2 = await store.write("b", _doc(), None)
    assert rev1 == rev2
    assert store.path_for("a").read_bytes() == store.path_for("b").read_bytes()


async def test_create_refuses_when_draft_exists(store):
    await store.write("a", _doc(), None)
    with pytest.raises(RevConflict) as exc:
        await store.write("a", _doc("Other"), None)
    assert exc.value.conflicts == ["a"]
    assert store.read("a")[0]["metadata"]["title"] == "Motor"


async def test_replace_needs_the_current_rev(store):
    rev = await store.write("a", _doc(), None)
    new_rev = await store.write("a", _doc("V2"), rev)
    assert new_rev != rev
    with pytest.raises(RevConflict):
        await store.write("a", _doc("V3"), rev)  # stale
    assert store.read("a") == (_doc("V2"), new_rev)


async def test_replace_of_missing_draft_is_a_conflict(store):
    with pytest.raises(RevConflict):
        await store.write("ghost", _doc(), "0" * 64)
    assert not store.path_for("ghost").exists()


async def test_write_refuses_invalid_id(store):
    with pytest.raises(InvalidDraftId):
        await store.write("../x", _doc(), None)


async def test_interleaved_writes_with_same_base_rev_exactly_one_wins(store):
    base = await store.write("race", _doc(), None)
    results = await asyncio.gather(
        store.write("race", _doc("from A"), base),
        store.write("race", _doc("from B"), base),
        return_exceptions=True,
    )
    wins = [r for r in results if isinstance(r, str)]
    losses = [r for r in results if isinstance(r, RevConflict)]
    assert len(wins) == 1 and len(losses) == 1
    doc, rev = store.read("race")
    assert rev == wins[0]
    assert doc["metadata"]["title"] in {"from A", "from B"}


async def test_interleaved_creates_exactly_one_wins(store):
    results = await asyncio.gather(
        store.write("new", _doc("A"), None), store.write("new", _doc("B"), None), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, RevConflict) for r in results) == 1


async def test_write_leaves_no_temp_files(store):
    rev = await store.write("a/b", _doc(), None)
    await store.write("a/b", _doc("V2"), rev)
    names = sorted(p.name for p in (store.root / "screens" / "a").iterdir())
    assert names == ["b.screen.json"]


def test_read_missing_raises(store):
    with pytest.raises(DraftNotFound):
        store.read("nope")
    assert store.rev("nope") is None


async def test_rev_tracks_external_edits(store):
    rev = await store.write("a", _doc(), None)
    path = store.path_for("a")
    path.write_text(json.dumps(_doc("edited by hand")))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert store.rev("a") != rev
    assert store.read("a")[0]["metadata"]["title"] == "edited by hand"


# --- write_many -------------------------------------------------------------


async def test_write_many_writes_all(store):
    revs = await store.write_many({"p": (_doc("P"), None), "p/child": (_doc("C"), None)})
    assert set(revs) == {"p", "p/child"}
    assert store.read("p/child") == (_doc("C"), revs["p/child"])


async def test_write_many_is_all_or_nothing_and_lists_every_conflict(store):
    rev_a = await store.write("a", _doc("A"), None)
    await store.write("b", _doc("B"), None)
    with pytest.raises(RevConflict) as exc:
        await store.write_many(
            {
                "a": (_doc("A2"), rev_a),  # fine
                "b": (_doc("B2"), None),  # exists
                "c": (_doc("C"), "f" * 64),  # missing
                "d": (_doc("D"), None),  # fine
            }
        )
    assert sorted(exc.value.conflicts) == ["b", "c"]
    assert store.read("a")[0]["metadata"]["title"] == "A"
    assert store.read("b")[0]["metadata"]["title"] == "B"
    assert not store.path_for("d").exists()


async def test_write_many_refuses_invalid_id_before_any_write(store):
    with pytest.raises(InvalidDraftId):
        await store.write_many({"ok": (_doc(), None), "bad/../x": (_doc(), None)})
    assert not store.path_for("ok").exists()


# --- delete / list ----------------------------------------------------------


async def test_list_returns_sorted_ids(store):
    assert store.list() == []
    for draft_id in ["b", "a/z", "a/b/c"]:
        await store.write(draft_id, _doc(), None)
    (store.root / "screens" / "a" / "notes.txt").write_text("x")
    (store.root / "screens" / "a" / "img.png").write_bytes(b"\x89PNG")
    assert store.list() == ["a/b/c", "a/z", "b"]


async def test_delete_removes_the_draft(store):
    rev = await store.write("a/b", _doc(), None)
    await store.delete("a/b", rev)
    assert store.list() == []
    assert not store.path_for("a/b").exists()
    with pytest.raises(DraftNotFound):
        await store.delete("a/b")


async def test_delete_with_stale_rev_is_a_conflict(store):
    rev = await store.write("a", _doc(), None)
    await store.write("a", _doc("V2"), rev)
    with pytest.raises(RevConflict):
        await store.delete("a", rev)
    assert store.path_for("a").exists()


# --- manifest ---------------------------------------------------------------


async def test_manifest_in_viewer_shape(store):
    assert store.manifest() == {"screens": []}
    rev_a = await store.write("a/b", _doc("Motor B"), None)
    rev_t = await store.write("top", _doc("Top"), None)
    assert store.manifest() == {
        "screens": [
            {
                "id": "a/b",
                "kind": "ir",
                "category": "drafts",
                "title": "Motor B",
                "path": "screens/a/b.screen.json",
                "rev": rev_a,
            },
            {
                "id": "top",
                "kind": "ir",
                "category": "drafts",
                "title": "Top",
                "path": "screens/top.screen.json",
                "rev": rev_t,
            },
        ]
    }


async def test_manifest_is_computed_per_call_and_tolerates_bad_files(store):
    await store.write("a", _doc("A"), None)
    store.path_for("broken").write_text("{not json")
    store.path_for("untitled").write_text(json.dumps({"kind": "screen"}))
    entries = {e["id"]: e for e in store.manifest()["screens"]}
    assert entries["a"]["title"] == "A"
    assert entries["broken"]["title"] == "broken"
    assert entries["untitled"]["title"] == "untitled"


# --- resolve_static ---------------------------------------------------------


async def test_resolve_static_serves_screens_and_images(store):
    await store.write("a/b", _doc(), None)
    img = store.root / "screens" / "a" / "logo.png"
    img.write_bytes(b"\x89PNG")
    assert store.resolve_static("screens/a/b.screen.json") == store.path_for("a/b").resolve()
    assert store.resolve_static("screens/a/logo.png") == img.resolve()
    assert store.resolve_static("screens/a/missing.png") is None


async def test_resolve_static_refuses_escapes_and_other_types(store, tmp_path):
    await store.write("a", _doc(), None)
    (store.root / "secret.txt").write_text("x")
    (tmp_path / "outside.png").write_bytes(b"\x89PNG")
    for rel in [
        "../outside.png",
        "screens/../../outside.png",
        "%2e%2e/outside.png",
        "screens/%2E%2E/%2e%2e/outside.png",
        "%252e%252e/outside.png",
        str(tmp_path / "outside.png"),
        "/etc/passwd",
        "secret.txt",
        "screens",
        "",
        "screens/a.screen.json\x00.png",
        "screens\\..\\..\\outside.png",
    ]:
        assert store.resolve_static(rel) is None, rel


async def test_resolve_static_refuses_symlink_escape(store, tmp_path):
    await store.write("a", _doc(), None)
    (tmp_path / "outside.png").write_bytes(b"\x89PNG")
    (store.root / "screens" / "link.png").symlink_to(tmp_path / "outside.png")
    assert store.resolve_static("screens/link.png") is None
