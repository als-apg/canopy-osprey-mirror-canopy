"""The manifest of an unchanged store: what it says, what it opens, and how a change shows.

``DraftStore.manifest()`` keeps each listed screen's rev, title and description
under the file's stat key ``(st_ino, st_mtime_ns, st_size)``, the rule the rev
cache follows. These tests hold it to three things:

* its answer is the one a from-scratch reading of the tree gives (``from_scratch``
  restates the listing rules with nothing remembered);
* a second call over an unchanged tree opens no screen file, and
  ``GET /drafts/manifest.json`` does not encode its body again;
* an edit, an addition, a removal, a rename and a rewrite of the same size are seen
  on the next call exactly where the rev cache sees them.
"""

import os
import sys
import json
import hashlib
from typing import Any
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import canopy_mcp.store as store_module
import canopy_mcp.manifest as manifest_module
from canopy_mcp.app import create_app
from canopy_mcp.store import (
    DRAFTS,
    ID_PATTERN,
    SCREEN_SUFFIX,
    DraftStore,
    contained_file,
    summarize_description,
)
from canopy_mcp.config import Settings

# --- counting opens ---------------------------------------------------------
# An audit hook sees every open, whichever call made it (``open``, ``os.open``,
# ``Path.read_bytes``). A hook cannot be removed, so one is installed for the process
# and records only while a test has handed it a list.

_OPENED: list[str] | None = None


def _audit(event: str, args: tuple) -> None:
    if _OPENED is not None and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        name = os.fsdecode(args[0])
        if name.endswith(SCREEN_SUFFIX):
            _OPENED.append(name)


sys.addaudithook(_audit)


@pytest.fixture
def opened():
    """The screen files opened since the fixture was set up, in order."""
    global _OPENED
    _OPENED = []
    try:
        yield _OPENED
    finally:
        _OPENED = None


# --- the fixture library ----------------------------------------------------


def _screen(title: Any = None, description: Any = None, **extra) -> str:
    metadata = {}
    if title is not None:
        metadata["title"] = title
    if description is not None:
        metadata["description"] = description
    return json.dumps({"schemaVersion": "1.0", "kind": "screen", "metadata": metadata, **extra})


def _put(root: Path, relpath: str, text: str) -> Path:
    path = root / "screens" / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path) -> Path:
    """A store root holding every kind of entry the listing has a rule for."""
    root = tmp_path / "store"
    _put(root, "top.screen.json", _screen("Top", "The first one. And more."))
    _put(root, "ring/overview.screen.json", _screen("Ring overview"))
    _put(root, "ring/magnets/quad.screen.json", _screen("Quadrupoles", "x" * 400))
    _put(root, "ring/magnets/untitled.screen.json", _screen())
    _put(root, "ring/magnets/empty-title.screen.json", _screen(""))
    _put(root, "ring/magnets/number-title.screen.json", _screen(7))
    _put(root, "ring/unicode.screen.json", _screen("Übersicht … é"))
    _put(root, "broken.screen.json", "{not json")
    _put(root, "not-utf8.screen.json", "").write_bytes(b"\xff\xfe{}")
    _put(root, "a-list.screen.json", "[1, 2]")
    _put(root, "metadata-list.screen.json", json.dumps({"metadata": ["x"]}))
    _put(root, "empty.screen.json", "")
    # Names no id can hold, and what is not a screen file at all.
    _put(root, "has.dot.screen.json", _screen("Dotted"))
    _put(root, "with space/x.screen.json", _screen("Spaced"))
    _put(root, ".hidden/x.screen.json", _screen("Hidden"))
    _put(root, "ring/notes.txt", "not a screen")
    _put(root, "ring/logo.png", "not a screen")
    (root / "screens" / "folder.screen.json").mkdir()
    _put(root, "folder.screen.json/inside.screen.json", _screen("Inside a folder named as a screen"))
    # The reserved first segment, at the top and deeper.
    _put(root, "library/x.screen.json", _screen("Reserved"))
    _put(root, "ring/library/x.screen.json", _screen("Not reserved here"))
    # Symlinks: to a screen inside, to a file outside, dangling, and to directories.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.screen.json").write_text(_screen("Outside"))
    screens = root / "screens"
    (screens / "alias.screen.json").symlink_to(screens / "top.screen.json")
    (screens / "escape.screen.json").symlink_to(outside / "secret.screen.json")
    (screens / "dangling.screen.json").symlink_to(screens / "nowhere.screen.json")
    (screens / "linked-in").symlink_to(screens / "ring", target_is_directory=True)
    (screens / "linked-out").symlink_to(outside, target_is_directory=True)
    return root


def from_scratch(store: DraftStore, *, descriptions: bool = False) -> dict[str, list[dict[str, Any]]]:
    """The manifest read from the tree with nothing remembered: every listed screen resolved, read and hashed."""
    prefix = "" if store.category == DRAFTS else f"{store.category}/"
    entries = {}
    for path in store.screens.rglob(f"*{SCREEN_SUFFIX}"):
        draft_id = path.relative_to(store.screens).as_posix()[: -len(SCREEN_SUFFIX)]
        if not ID_PATTERN.fullmatch(draft_id) or draft_id.split("/")[0] in store.reserved:
            continue
        target = contained_file(store.root, path.relative_to(store.root))
        if target is None:
            continue
        data = target.read_bytes()
        try:
            doc = json.loads(data)
        except ValueError:
            doc = None
        metadata = doc.get("metadata") if isinstance(doc, dict) else None
        if not isinstance(metadata, dict):
            metadata = {}
        title = metadata.get("title")
        entry = {
            "id": draft_id,
            "kind": "ir",
            "category": store.category,
            "title": title if isinstance(title, str) and title else draft_id,
            "path": prefix + path.relative_to(store.root).as_posix(),
            "rev": hashlib.sha256(data).hexdigest(),
        }
        if descriptions:
            entry["description"] = summarize_description(metadata.get("description"))
        entries[draft_id] = entry
    return {"screens": [entries[draft_id] for draft_id in sorted(entries)]}


STORES = {
    "drafts": lambda root: DraftStore(root, reserved=("library",)),
    "library": lambda root: DraftStore(root, read_only=True, category="library"),
}


@pytest.fixture(params=sorted(STORES))
def store(request, tree) -> DraftStore:
    return STORES[request.param](tree)


def _rewrite_in_place(path: Path, text: str, *, mtime_ns: int | None = None) -> None:
    """Write ``text`` into the existing file (same inode); ``mtime_ns`` sets the modification time after it."""
    with open(path, "r+", encoding="utf-8") as fh:
        fh.truncate(0)
        fh.write(text)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


# --- the same answer --------------------------------------------------------


def test_the_fixture_lists_what_the_rules_say(tree):
    """``from_scratch`` is only a reference if it lists the fixture as the rules do; this names the ids outright."""
    listed = [entry["id"] for entry in from_scratch(DraftStore(tree, reserved=("library",)))["screens"]]
    assert listed == [
        "a-list",
        "alias",
        "broken",
        "empty",
        "metadata-list",
        "not-utf8",
        "ring/library/x",
        "ring/magnets/empty-title",
        "ring/magnets/number-title",
        "ring/magnets/quad",
        "ring/magnets/untitled",
        "ring/overview",
        "ring/unicode",
        "top",
    ]
    library = [entry["id"] for entry in from_scratch(DraftStore(tree, read_only=True, category="library"))["screens"]]
    assert library == sorted([*listed, "library/x"])


@pytest.mark.parametrize("descriptions", [False, True], ids=["viewer", "descriptions"])
def test_manifest_is_the_from_scratch_listing_on_every_call(store, descriptions):
    expected = from_scratch(store, descriptions=descriptions)
    assert expected["screens"]
    for _ in range(3):
        assert store.manifest(descriptions=descriptions) == expected


def test_the_two_shapes_share_what_is_remembered(store, opened):
    store.manifest()
    del opened[:]
    described = store.manifest(descriptions=True)
    plain = store.manifest()
    assert opened == []
    assert described == from_scratch(store, descriptions=True)
    assert plain == from_scratch(store)


def test_list_is_the_ids_of_the_manifest(store):
    assert store.list() == [entry["id"] for entry in from_scratch(store)["screens"]]


def test_a_symlinked_screens_directory_is_listed_as_it_resolves(tmp_path):
    inside = tmp_path / "store"
    _put(inside, "x.screen.json", _screen("Inside")).parent.rename(inside / "kept")
    (inside / "screens").symlink_to(inside / "kept", target_is_directory=True)
    store = DraftStore(inside)
    for _ in range(2):
        assert store.manifest() == from_scratch(store)
        assert [entry["id"] for entry in store.manifest()["screens"]] == ["x"]

    outside = tmp_path / "elsewhere"
    _put(outside, "y.screen.json", _screen("Outside"))
    leaving = tmp_path / "leaving"
    leaving.mkdir()
    (leaving / "screens").symlink_to(outside / "screens", target_is_directory=True)
    store = DraftStore(leaving)
    for _ in range(2):
        assert store.manifest() == {"screens": []}
        assert store.list() == []


# --- what an unchanged store costs ------------------------------------------


def test_the_first_manifest_opens_each_listed_screen_once(store, opened):
    listed = store.manifest()["screens"]
    seen = sorted(os.path.realpath(name) for name in opened)
    # ``alias`` is a link to ``top``, so that file is read under both ids.
    assert seen == sorted(os.path.realpath(store.path_for(entry["id"])) for entry in listed)


def test_an_unchanged_store_opens_no_screen_on_the_next_manifest(store, opened):
    first = store.manifest()
    assert len(opened) >= len(first["screens"])
    del opened[:]
    assert store.manifest() == first
    assert store.manifest(descriptions=True)["screens"]
    assert opened == []


def test_only_the_changed_screen_is_opened(store, opened):
    store.manifest()
    path = store.path_for("ring/overview")
    _put(store.root, "ring/overview.screen.json", _screen("Ring overview, edited"))
    del opened[:]
    after = store.manifest()
    assert [os.path.realpath(name) for name in opened] == [os.path.realpath(path)]
    assert after == from_scratch(store)


def test_what_is_remembered_follows_the_listing(tree):
    store = DraftStore(tree, reserved=("library",))
    listed = len(store.manifest()["screens"])
    assert len(store._listed) == listed
    store.path_for("ring/overview").unlink()
    store.path_for("top").unlink()  # takes the ``alias`` link's target with it
    assert len(store.manifest()["screens"]) == listed - 3
    assert len(store._listed) == listed - 3
    # Titles and revs only: nothing remembered holds a document.
    assert all(isinstance(value, (int, str)) for entry in store._listed.values() for value in (*entry[0], *entry[1:]))


def test_a_screen_that_cannot_be_read_is_left_alone_until_it_changes(tree, opened, monkeypatch):
    store = DraftStore(tree, reserved=("library",))
    path = store.path_for("ring/overview")
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("this user reads the file whatever its mode")
        ids = [entry["id"] for entry in store.manifest()["screens"]]
        assert "ring/overview" not in ids
        del opened[:]
        for _ in range(3):
            assert "ring/overview" not in [entry["id"] for entry in store.manifest()["screens"]]
        assert opened == []
        # Past the retry time it is tried once more, as it is.
        monkeypatch.setattr(store_module, "FAILED_RETRY_S", 0.0)
        store.manifest()
        assert [os.path.realpath(name) for name in opened] == [os.path.realpath(path)]
        monkeypatch.setattr(store_module, "FAILED_RETRY_S", 60.0)
        del opened[:]
        store.manifest()
        assert opened == []
    finally:
        path.chmod(0o644)
    # The mode is part of what the failure was remembered under: readable again, it is read at once.
    assert store.manifest() == from_scratch(store)


# --- a change shows on the next call ----------------------------------------


def test_an_edit_shows(store):
    store.manifest()
    _put(store.root, "ring/overview.screen.json", _screen("Ring overview, edited", "Now described."))
    assert store.manifest() == from_scratch(store)
    assert store.manifest(descriptions=True) == from_scratch(store, descriptions=True)
    titles = {entry["id"]: entry["title"] for entry in store.manifest()["screens"]}
    assert titles["ring/overview"] == "Ring overview, edited"


def test_an_added_screen_and_an_added_folder_show(store):
    before = store.manifest()
    _put(store.root, "ring/added.screen.json", _screen("Added"))
    _put(store.root, "new/deep/folder/added.screen.json", _screen("Added deep"))
    after = store.manifest()
    assert after == from_scratch(store)
    assert {entry["id"] for entry in after["screens"]} - {entry["id"] for entry in before["screens"]} == {
        "ring/added",
        "new/deep/folder/added",
    }


def test_a_removed_screen_and_a_removed_folder_show(store):
    store.manifest()
    store.path_for("ring/overview").unlink()
    for path in sorted((store.screens / "ring" / "magnets").iterdir()):
        path.unlink()
    (store.screens / "ring" / "magnets").rmdir()
    after = store.manifest()
    assert after == from_scratch(store)
    assert not [entry["id"] for entry in after["screens"] if entry["id"].startswith(("ring/overview", "ring/magnets"))]


def test_a_renamed_screen_and_a_renamed_folder_show(store):
    before = {entry["id"]: entry for entry in store.manifest()["screens"]}
    store.path_for("ring/overview").rename(store.screens / "ring" / f"moved{SCREEN_SUFFIX}")
    (store.screens / "ring" / "magnets").rename(store.screens / "ring" / "supplies")
    after = {entry["id"]: entry for entry in store.manifest()["screens"]}
    assert list(after.values()) == from_scratch(store)["screens"]
    assert "ring/overview" not in after and "ring/magnets/quad" not in after
    assert after["ring/moved"]["rev"] == before["ring/overview"]["rev"]
    assert after["ring/moved"]["title"] == "Ring overview"
    assert after["ring/supplies/quad"]["title"] == "Quadrupoles"
    # No title of its own: the title is the id, so it follows the rename.
    assert after["ring/supplies/untitled"]["title"] == "ring/supplies/untitled"


def test_a_screen_swapped_for_another_under_its_name_shows(store):
    store.manifest()
    os.replace(store.path_for("ring/unicode"), store.path_for("ring/overview"))
    after = store.manifest()
    assert after == from_scratch(store)
    assert {entry["id"]: entry["title"] for entry in after["screens"]}["ring/overview"] == "Übersicht … é"


def test_the_whole_folder_rewritten_shows(store):
    """A rebuild replaces every file: same names, new files, some of them with other content."""
    before = store.manifest()
    for entry in before["screens"]:
        path = store.path_for(entry["id"])
        if path.is_symlink():
            continue
        text = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        fresh = path.with_name(path.name + ".new")
        fresh.write_bytes(text.replace(b"Ring overview", b"Ring 0verview"))
        os.utime(fresh, ns=(stamp, stamp))
        os.replace(fresh, path)
    after = store.manifest()
    assert after == from_scratch(store)
    assert {entry["id"]: entry["title"] for entry in after["screens"]}["ring/overview"] == "Ring 0verview"


def test_a_rewrite_of_the_same_size_in_the_same_second_shows_when_it_is_a_new_file(store):
    path = store.path_for("ring/overview")
    stamp = path.stat().st_mtime_ns
    before = {entry["id"]: entry for entry in store.manifest()["screens"]}["ring/overview"]
    assert store.rev("ring/overview") == before["rev"]
    fresh = path.with_name("fresh.tmp")
    fresh.write_text(_screen("Ring 0verview"), encoding="utf-8")
    assert fresh.stat().st_size == path.stat().st_size
    os.utime(fresh, ns=(stamp, stamp))
    os.replace(fresh, path)
    assert path.stat().st_mtime_ns == stamp
    after = {entry["id"]: entry for entry in store.manifest()["screens"]}["ring/overview"]
    assert after["title"] == "Ring 0verview"
    assert after["rev"] == store.rev("ring/overview") != before["rev"]
    assert store.manifest() == from_scratch(store)


def test_a_rewrite_of_the_same_size_in_place_shows_when_its_time_moved(store):
    path = store.path_for("ring/overview")
    stamp = path.stat().st_mtime_ns
    store.manifest()
    # One nanosecond is a change of the key; a second survives a file system with coarse stamps.
    _rewrite_in_place(path, _screen("Ring 0verview"), mtime_ns=stamp + 1_000_000_000)
    after = {entry["id"]: entry for entry in store.manifest()["screens"]}["ring/overview"]
    assert after["title"] == "Ring 0verview"
    assert after["rev"] == store.rev("ring/overview")
    assert store.manifest() == from_scratch(store)


def test_a_rewrite_the_stat_key_does_not_show_is_where_the_rev_cache_leaves_it(store):
    """Same file, same size, same modification time: the key the rev cache trusts is unchanged.

    The manifest and ``rev()`` must agree here too. Neither sees the rewrite; a
    store with nothing remembered does.
    """
    path = store.path_for("ring/overview")
    stamp = path.stat().st_mtime_ns
    before = {entry["id"]: entry for entry in store.manifest()["screens"]}["ring/overview"]
    assert store.rev("ring/overview") == before["rev"]
    _rewrite_in_place(path, _screen("Ring 0verview"), mtime_ns=stamp)
    after = {entry["id"]: entry for entry in store.manifest()["screens"]}["ring/overview"]
    assert after == before
    assert store.rev("ring/overview") == before["rev"]
    assert STORES[store.category](store.root).manifest() == from_scratch(store)


async def test_a_write_through_the_store_shows(tree):
    store = DraftStore(tree, reserved=("library",))
    store.manifest()
    _, rev = store.read("ring/overview")
    new_rev = await store.write("ring/overview", json.loads(_screen("Written")), rev)
    await store.write("ring/created", json.loads(_screen("Created")), None)
    await store.delete("top")
    after = store.manifest()
    assert after == from_scratch(store)
    entries = {entry["id"]: entry for entry in after["screens"]}
    assert entries["ring/overview"]["rev"] == new_rev
    assert entries["ring/overview"]["title"] == "Written"
    assert "top" not in entries and "alias" not in entries


def test_a_listed_screen_that_becomes_a_link_out_of_the_store_is_dropped(store, tmp_path):
    store.manifest()
    path = store.path_for("ring/overview")
    path.unlink()
    path.symlink_to(tmp_path / "outside" / "secret.screen.json")
    after = store.manifest()
    assert after == from_scratch(store)
    assert "ring/overview" not in [entry["id"] for entry in after["screens"]]


def test_a_listed_screen_that_becomes_a_link_inside_the_store_follows_it(store):
    store.manifest()
    path = store.path_for("ring/overview")
    path.unlink()
    path.symlink_to(store.path_for("ring/unicode"))
    for _ in range(2):
        after = store.manifest()
        assert after == from_scratch(store)
    assert {entry["id"]: entry["title"] for entry in after["screens"]}["ring/overview"] == "Übersicht … é"
    # The link's target changes; the link itself does not.
    _put(store.root, "ring/unicode.screen.json", _screen("Target edited"))
    after = store.manifest()
    assert after == from_scratch(store)
    assert {entry["id"]: entry["title"] for entry in after["screens"]}["ring/overview"] == "Target edited"


def test_a_folder_that_becomes_a_link_is_no_longer_walked(store, tmp_path):
    store.manifest()
    magnets = store.screens / "ring" / "magnets"
    magnets.rename(tmp_path / "outside" / "magnets")
    magnets.symlink_to(tmp_path / "outside" / "magnets", target_is_directory=True)
    after = store.manifest()
    assert after == from_scratch(store)
    assert not [entry["id"] for entry in after["screens"] if entry["id"].startswith("ring/magnets/")]


# --- the route --------------------------------------------------------------


@pytest.fixture
def served(tmp_path, tree, monkeypatch):
    """A client over a personal store and the fixture tree as its library, and the count of manifest encodings."""
    drafts = tmp_path / "drafts"
    _put(drafts, "mine.screen.json", _screen("Mine"))
    encodings = []
    real_dumps = json.dumps

    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and set(value) == {"screens"}:
            encodings.append(len(value["screens"]))
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(manifest_module.json, "dumps", dumps)
    settings = Settings(drafts_dir=drafts, library_dir=tree, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        yield client, drafts, tree, encodings


def _expected_body(drafts: Path, library: Path) -> bytes:
    listing = [
        *from_scratch(DraftStore(drafts, reserved=("library",)))["screens"],
        *from_scratch(DraftStore(library, read_only=True, category="library"))["screens"],
    ]
    return json.dumps({"screens": listing}, ensure_ascii=False).encode("utf-8")


def test_route_serves_the_from_scratch_manifest_with_its_hash_as_etag(served):
    client, drafts, library, _ = served
    for _ in range(2):
        r = client.get("/drafts/manifest.json")
        assert r.status_code == 200
        assert r.content == _expected_body(drafts, library)
        assert r.headers["etag"] == f'"{hashlib.sha256(r.content).hexdigest()}"'
        assert r.headers["cache-control"] == "no-cache"


def test_route_revalidates_an_unchanged_store_without_opening_or_encoding(served, opened):
    client, _, _, encodings = served
    tag = client.get("/drafts/manifest.json").headers["etag"]
    assert len(encodings) == 1
    del opened[:]
    for _ in range(3):
        r = client.get("/drafts/manifest.json", headers={"if-none-match": tag})
        assert r.status_code == 304
        assert r.headers["etag"] == tag
    assert client.get("/drafts/manifest.json").headers["etag"] == tag
    assert opened == []
    assert len(encodings) == 1


@pytest.mark.parametrize("where", ["drafts", "library"])
def test_route_answers_a_change_in_either_store_with_a_new_body(served, where):
    client, drafts, library, encodings = served
    first = client.get("/drafts/manifest.json")
    tag = first.headers["etag"]
    root = drafts if where == "drafts" else library
    _put(root, "added.screen.json", _screen("Added"))
    changed = client.get("/drafts/manifest.json", headers={"if-none-match": tag})
    assert len(encodings) == 2
    assert changed.status_code == 200
    assert changed.headers["etag"] != tag
    assert changed.content == _expected_body(drafts, library)
    (root / "screens" / "added.screen.json").unlink()
    back = client.get("/drafts/manifest.json", headers={"if-none-match": tag})
    assert back.status_code == 304
    assert back.headers["etag"] == tag
