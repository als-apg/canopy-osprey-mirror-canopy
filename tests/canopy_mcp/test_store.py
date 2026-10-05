"""The draft store: id rules, compare-and-replace writes, revs, the manifest and the static path guard."""

import os
import json
import asyncio
import logging

import pytest

import canopy_mcp.store as store_module
from canopy_mcp.store import (
    MAX_ASSET_BYTES,
    DraftStore,
    RevConflict,
    AssetRefused,
    DraftNotFound,
    InvalidDraftId,
    looks_like_image,
    asset_relpath_reason,
)


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


# --- manifest warnings ------------------------------------------------------
# The viewer polls the manifest every second, so an unreadable draft must warn once per
# (path, exception type) per process — not once per poll, and not once per DraftStore.

STORE_LOGGER = "canopy_mcp.store"


@pytest.fixture(autouse=True)
def _fresh_manifest_warnings(monkeypatch):
    monkeypatch.setattr(store_module, "_MANIFEST_WARNED", set())


def _manifest_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == STORE_LOGGER and r.levelno == logging.WARNING]


def _fail_read_for(monkeypatch, draft_id: str, exc: BaseException) -> None:
    real_read = DraftStore.read

    def read(self, wanted):
        if wanted == draft_id:
            raise exc
        return real_read(self, wanted)

    monkeypatch.setattr(DraftStore, "read", read)


async def test_manifest_warns_once_for_non_json_and_keeps_entry(store, tmp_path, caplog):
    await store.write("a", _doc("A"), None)
    store.path_for("broken").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        first = {e["id"]: e for e in store.manifest()["screens"]}
        second = {e["id"]: e for e in store.manifest()["screens"]}
        # A second store over the same tree is the same process: still no second warning.
        third = {e["id"]: e for e in DraftStore(tmp_path / "drafts").manifest()["screens"]}
    for entries in (first, second, third):
        assert entries["broken"]["title"] == "broken"
        assert entries["broken"]["rev"] == store.rev("broken")
        assert entries["a"]["title"] == "A"
    warnings = _manifest_warnings(caplog)
    assert len(warnings) == 1
    assert str(store.path_for("broken")) in warnings[0].getMessage()


async def test_manifest_warns_once_for_os_error_and_drops_entry(store, tmp_path, monkeypatch, caplog):
    await store.write("a", _doc("A"), None)
    await store.write("locked", _doc("Locked"), None)
    _fail_read_for(monkeypatch, "locked", PermissionError(13, "Permission denied"))
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        ids = [[e["id"] for e in store.manifest()["screens"]] for _ in range(3)]
        ids.append([e["id"] for e in DraftStore(tmp_path / "drafts").manifest()["screens"]])
    assert ids == [["a"]] * 4
    warnings = _manifest_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(store.path_for("locked")) in message
    assert "PermissionError" in message


async def test_manifest_warns_once_per_exception_type_on_one_path(store, monkeypatch, caplog):
    await store.write("flaky", _doc("Flaky"), None)
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        _fail_read_for(monkeypatch, "flaky", PermissionError(13, "Permission denied"))
        store.manifest()
        store.manifest()
        _fail_read_for(monkeypatch, "flaky", IsADirectoryError(21, "Is a directory"))
        store.manifest()
        store.manifest()
    warnings = _manifest_warnings(caplog)
    assert len(warnings) == 2
    assert "PermissionError" in warnings[0].getMessage()
    assert "IsADirectoryError" in warnings[1].getMessage()


@pytest.mark.parametrize(
    "exc",
    [FileNotFoundError(2, "No such file or directory"), DraftNotFound("gone")],
    ids=["FileNotFoundError", "DraftNotFound"],
)
async def test_manifest_never_warns_for_a_vanished_draft(store, monkeypatch, caplog, exc):
    await store.write("a", _doc("A"), None)
    await store.write("gone", _doc("Gone"), None)
    _fail_read_for(monkeypatch, "gone", exc)
    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER):
        ids = [e["id"] for e in store.manifest()["screens"]]
        store.manifest()
    assert ids == ["a"]
    assert _manifest_warnings(caplog) == []


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


# --- write_asset: the single enforcement point for images copied into the store ---

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.mark.parametrize(
    "relpath",
    [
        "../x.png",
        "a/../../x.png",
        "%2e%2e/x.png",
        "/x.png",
        "~/x.png",
        "a/x.bob",
        "${IMG}.png",
        "x.screen.json/a.png",
        "a/X.Screen.Json",
        "a\\x.png",
        "a/\x00x.png",
        "",
        "a//x.png",
        "./x.png",
        "a/x.txt",
        "a/x",
    ],
)
async def test_write_asset_invalid_ref(store, relpath) -> None:
    assert asset_relpath_reason(relpath) == "invalid_ref"
    with pytest.raises(AssetRefused) as e:
        await store.write_asset(relpath, PNG)
    assert e.value.reason == "invalid_ref"
    assert isinstance(e.value, ValueError)
    assert not store.screens.exists()


@pytest.mark.parametrize("relpath", ["a/x.png", "Logo.PNG", "a b/c-d_e.1.jpeg", "a/Logo.SVG"])
def test_asset_relpath_reason_accepts(relpath) -> None:
    assert asset_relpath_reason(relpath) is None


async def test_write_asset_too_large_before_byte_check(store) -> None:
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", b"x" * (6 * 2**20))
    assert e.value.reason == "too_large"
    assert not (store.screens / "a" / "x.png").exists()


async def test_write_asset_at_limit_is_written(store) -> None:
    data = PNG + b"\x00" * (MAX_ASSET_BYTES - len(PNG))
    assert await store.write_asset("a/x.png", data) == "written"


@pytest.mark.parametrize(
    "data,message",
    [
        (b"VAR=1\x00OTHER=2", "PNG/JPEG/GIF/BMP/ICO/WEBP"),
        (b"just some text " * 7, "PNG/JPEG/GIF/BMP/ICO/WEBP"),
    ],
)
async def test_write_asset_not_image(store, data, message) -> None:
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", data)
    assert e.value.reason == "not_image"
    assert message in str(e.value)
    assert not (store.screens / "a" / "x.png").exists()


async def test_write_asset_svg_not_image_message(store) -> None:
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.svg", PNG)
    assert e.value.reason == "not_image"
    assert "not an SVG document" in str(e.value)


@pytest.mark.parametrize(
    "relpath,data",
    [
        ("a/x.png", PNG),
        ("a/x.jpg", JPEG),
        ("a/x.gif", GIF),
        ("a/x.svg", SVG),
        ("a/x.svg", b"\xef\xbb\xbf \n\t<?xml version='1.0'?><svg/>"),
        ("a/x.jpg", PNG),  # a mislabeled PNG is still an image
        ("a/Logo.SVG", SVG),
        ("Logo.PNG", PNG),
        ("a/x.bmp", b"BM" + b"\x00" * 16),
        ("a/x.ico", b"\x00\x00\x01\x00" + b"\x00" * 16),
        ("a/x.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8),
    ],
)
async def test_write_asset_written(store, relpath, data) -> None:
    assert await store.write_asset(relpath, data) == "written"
    assert (store.screens / relpath).read_bytes() == data
    assert store.resolve_static(f"screens/{relpath}") == (store.screens / relpath).resolve()


def test_looks_like_image_union() -> None:
    assert looks_like_image(PNG, ".png")
    assert looks_like_image(JPEG, ".png")  # extension/format match is not policed
    assert not looks_like_image(b"RIFF\x00\x00\x00\x00WAVE", ".webp")
    assert not looks_like_image(SVG, ".png")
    assert not looks_like_image(b"", ".png")
    assert not looks_like_image(b"   ", ".svg")
    assert not looks_like_image(PNG, ".txt")


async def test_write_asset_reused_replaced_conflict(store) -> None:
    assert await store.write_asset("a/x.png", PNG) == "written"
    assert await store.write_asset("a/x.png", PNG) == "reused"
    same_size = PNG[:-1] + b"\x01"
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", same_size)
    assert e.value.reason == "conflict"
    assert "overwrite=true" in str(e.value)
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", PNG + b"more")
    assert e.value.reason == "conflict"
    assert (store.screens / "a" / "x.png").read_bytes() == PNG
    assert await store.write_asset("a/x.png", same_size, overwrite=True) == "replaced"
    assert (store.screens / "a" / "x.png").read_bytes() == same_size
    assert await store.write_asset("a/x.png", same_size, overwrite=True) == "reused"


async def test_write_asset_symlinked_leaf_is_conflict(store, tmp_path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (store.screens / "a").mkdir(parents=True)
    (store.screens / "a" / "x.png").symlink_to(outside)
    for overwrite in (False, True):
        with pytest.raises(AssetRefused) as e:
            await store.write_asset("a/x.png", PNG, overwrite=overwrite)
        assert e.value.reason == "conflict"
        assert "symlink" in str(e.value)
    assert (store.screens / "a" / "x.png").is_symlink()
    assert outside.read_bytes() == PNG


async def test_write_asset_directory_at_target_is_conflict(store) -> None:
    (store.screens / "a" / "x.png").mkdir(parents=True)
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", PNG, overwrite=True)
    assert e.value.reason == "conflict"
    assert "directory" in str(e.value)


async def test_write_asset_screens_symlinked_outside_is_conflict(store, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.root.mkdir()
    store.screens.symlink_to(elsewhere)
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("a/x.png", PNG)
    assert e.value.reason == "conflict"
    assert str(elsewhere.resolve()) in str(e.value)
    assert list(elsewhere.iterdir()) == []


async def test_write_asset_intermediate_symlinked_outside_is_conflict(store, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.screens.mkdir(parents=True)
    (store.screens / "bts").symlink_to(elsewhere)
    with pytest.raises(AssetRefused) as e:
        await store.write_asset("bts/sub/x.png", PNG)
    assert e.value.reason == "conflict"
    assert list(elsewhere.iterdir()) == []


async def test_write_asset_dangling_screens_link_is_oserror(store, tmp_path) -> None:
    store.root.mkdir()
    store.screens.symlink_to(tmp_path / "gone")
    with pytest.raises(FileExistsError):
        await store.write_asset("a/x.png", PNG)
    assert not (tmp_path / "gone").exists()


async def test_write_asset_symlinked_root_writes_and_serves(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store = DraftStore(link)
    assert await store.write_asset("bts/logo.png", PNG) == "written"
    assert (real / "screens" / "bts" / "logo.png").read_bytes() == PNG
    served = store.resolve_static("screens/bts/logo.png")
    assert served is not None and served.read_bytes() == PNG


async def test_write_asset_holds_the_store_lock(store) -> None:
    async with store._lock:
        task = asyncio.ensure_future(store.write_asset("a/x.png", PNG))
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not (store.screens / "a" / "x.png").exists()
    assert await task == "written"


def test_resolve_static_still_decodes_and_serves_screens(store) -> None:
    path = store.screens / "a" / "v.screen.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert store.resolve_static("screens/a/v%2escreen.json") == path.resolve()
    assert store.resolve_static("screens/%2e%2e/a/v.screen.json") is None
