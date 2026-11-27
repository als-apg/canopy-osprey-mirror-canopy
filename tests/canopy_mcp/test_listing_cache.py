"""A store's listing between runs and between requests: the cache file, the maximum age, a failed read.

``test_manifest_cache`` holds the listing to the tree on every call. This module
holds the three ways a listing avoids work:

* a new process lists a known tree from the listing cache file without opening a
  screen, a tree staged again with the same files included, and what the file
  cannot vouch for is read afterwards (``verify_adopted``);
* a listing younger than its maximum age is answered without looking at the
  disk, and a write through the store shows at once all the same;
* a screen that could not be read is left alone while it stays as it is.
"""

# Fixtures are imported from the modules that define them, and a test names them as arguments.
# ruff: noqa: F811

import os
import json
import hashlib
from pathlib import Path

import pytest

import canopy_mcp.store as store_module
from canopy_mcp.store import DraftStore

from .test_manifest_cache import _put, opened, _screen, from_scratch  # noqa: F401


@pytest.fixture
def roots(tmp_path) -> tuple[Path, Path]:
    """A drafts root with one screen and a library root with three, in folders."""
    drafts, library = tmp_path / "drafts", tmp_path / "library"
    _put(drafts, "mine.screen.json", _screen("Mine", "My own."))
    _put(library, "top.screen.json", _screen("Top"))
    _put(library, "ring/overview.screen.json", _screen("Ring overview", "The ring. And more."))
    _put(library, "ring/magnets/quad.screen.json", _screen("Quadrupoles"))
    return drafts, library


def count_walks(monkeypatch: pytest.MonkeyPatch, *stores: DraftStore) -> list[str]:
    """The category of each store whose tree is walked from now on, in order."""
    walks: list[str] = []
    for store in stores:
        real = store._walk

        def walk(real=real, store=store):
            walks.append(store.category)
            return real()

        monkeypatch.setattr(store, "_walk", walk)
    return walks


# --- the listing cache file ---------------------------------------------------------------


def _library(root: Path, cache: Path | None) -> DraftStore:
    return DraftStore(root, read_only=True, category="library", cache_file=cache)


def test_a_new_process_lists_a_known_tree_without_opening_a_screen(tmp_path, roots, opened):
    _, library = roots
    cache = tmp_path / "cache" / "listing.json"
    cache.parent.mkdir()
    first = _library(library, cache).manifest(descriptions=True)
    assert len(opened) == 3 and cache.is_file()
    assert [p.name for p in cache.parent.iterdir()] == [cache.name]  # no temp file is left
    del opened[:]
    again = _library(library, cache)
    assert again.manifest(descriptions=True) == first
    assert opened == []
    assert first == from_scratch(again, descriptions=True)
    del opened[:]
    # The very files that were read (same inode, same change time) need no second look,
    assert again.unverified == 0
    # and serving one does not hash it again.
    assert again.rev("ring/overview") == first["screens"][1]["rev"]
    assert opened == []


def test_only_what_changed_since_the_cache_file_is_read(tmp_path, roots, opened):
    _, library = roots
    cache = tmp_path / "listing.json"
    _library(library, cache).manifest()
    _put(library, "ring/overview.screen.json", _screen("Ring overview, edited at another length"))
    _put(library, "ring/added.screen.json", _screen("Added"))
    (library / "screens" / "top.screen.json").unlink()
    del opened[:]
    store = _library(library, cache)
    listed = store.manifest()
    assert sorted(Path(name).name for name in opened) == ["added.screen.json", "overview.screen.json"]
    assert listed == from_scratch(store)
    # What it found is in the file for the next process.
    del opened[:]
    assert _library(library, cache).manifest() == listed
    assert opened == []


def _restage(root: Path, edit=lambda data: data) -> None:
    """Replace every screen by a new file (a new inode) of the same size and modification time."""
    for path in sorted((root / "screens").rglob("*.screen.json")):
        stamp = path.stat().st_mtime_ns
        fresh = path.with_name(path.name + ".new")
        data = edit(path.read_bytes())
        assert len(data) == path.stat().st_size
        fresh.write_bytes(data)
        os.utime(fresh, ns=(stamp, stamp))
        os.replace(fresh, path)


def test_a_tree_staged_again_with_the_same_files_is_not_read_to_be_listed(tmp_path, roots, opened):
    _, library = roots
    cache = tmp_path / "listing.json"
    first = _library(library, cache).manifest()
    _restage(library)
    del opened[:]
    store = _library(library, cache)
    assert store.manifest() == first
    assert opened == []
    # New inodes: the file cannot vouch for these, so they wait for the background reading,
    assert store.unverified == 3
    assert store.verify_adopted() == 0
    assert store.unverified == 0 and len(opened) == 3
    # which also saves hashing them when they are served.
    del opened[:]
    assert store.rev("top") == first["screens"][-1]["rev"]
    assert opened == []
    # The next process finds the new inodes in the file.
    assert _library(library, cache).manifest() == first
    assert _library(library, cache).unverified == 0


def test_bytes_changed_under_the_same_size_and_time_are_found_by_the_background_reading(tmp_path, roots):
    _, library = roots
    cache = tmp_path / "listing.json"
    first = _library(library, cache).manifest()
    _restage(library, lambda data: data.replace(b"Ring overview", b"Ring 0verview"))
    store = _library(library, cache)
    # The trade-off, stated: until it is read, the screen is listed as the file recorded it,
    assert store.manifest() == first
    # yet it is never served under a rev that is not the one of its bytes.
    assert store.rev("ring/overview") == hashlib.sha256(store.path_for("ring/overview").read_bytes()).hexdigest()
    assert store.verify_adopted() == 1
    # The next listing reads it, even one that would be young enough to be answered again.
    after = store.manifest(max_age=3600)
    assert after == from_scratch(store) != first
    assert {s["id"]: s["title"] for s in after["screens"]}["ring/overview"] == "Ring 0verview"
    fresh = _library(library, cache)
    assert fresh.manifest() == after and fresh.unverified == 0


@pytest.mark.parametrize(
    "content",
    [b"", b"{not json", b"[1, 2]", b'{"version": 0, "screens": {}}', b'{"version": 1, "screens": {"top": [1, 2]}}'],
    ids=["empty", "broken", "a-list", "another-version", "a-short-row"],
)
def test_a_cache_file_that_cannot_be_used_is_no_cache(tmp_path, roots, content):
    _, library = roots
    cache = tmp_path / "listing.json"
    cache.write_bytes(content)
    store = _library(library, cache)
    assert store.manifest() == from_scratch(store)
    assert json.loads(cache.read_bytes())["version"] == store_module.LISTING_CACHE_VERSION


def test_a_cache_file_that_names_another_rev_shape_is_not_believed(tmp_path, roots):
    _, library = roots
    cache = tmp_path / "listing.json"
    store = _library(library, cache)
    store.manifest()
    doc = json.loads(cache.read_bytes())
    doc["screens"]["top"][4] = "not-a-rev"
    cache.write_text(json.dumps(doc))
    fresh = _library(library, cache)
    assert fresh.manifest() == from_scratch(fresh)


def test_a_cache_that_cannot_be_written_fails_no_listing(tmp_path, roots, caplog):
    _, library = roots
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    store = _library(library, blocked / "cache" / "listing.json")
    with caplog.at_level("INFO", logger="canopy_mcp.store"):
        for _ in range(2):
            assert store.manifest() == from_scratch(store)
        _put(library, "ring/added.screen.json", _screen("Added"))
        assert store.manifest() == from_scratch(store)
    assert len([r for r in caplog.records if "not written" in r.getMessage()]) == 1


def test_listing_does_not_create_a_store_that_is_not_there(tmp_path):
    root = tmp_path / "absent"
    store = DraftStore(root, cache_file=root / ".cache" / "listing.json")
    assert store.manifest() == {"screens": []}
    assert not root.exists()


# --- a maximum age ------------------------------------------------------------------------


def test_a_young_listing_is_answered_again_without_a_walk(roots, monkeypatch):
    _, library = roots
    store = _library(library, None)
    walks = count_walks(monkeypatch, store)
    first = store.manifest(max_age=3600)
    assert walks == ["library"]
    # What another process does to the tree is not seen while the listing is young,
    _put(library, "ring/added.screen.json", _screen("Added"))
    for _ in range(3):
        assert store.manifest(max_age=3600) == first
        assert store.manifest(descriptions=True, max_age=3600)["screens"]
    assert walks == ["library"]
    # and is seen by whoever asks for the tree as it is, or once the listing is old.
    assert store.manifest() == from_scratch(store) != first
    assert store.manifest(max_age=1e-9) == from_scratch(store)
    assert len(walks) == 3


async def test_a_write_through_the_store_shows_whatever_the_age(roots, monkeypatch):
    drafts, _ = roots
    store = DraftStore(drafts)
    first = store.manifest(max_age=3600)
    rev = await store.write("added", json.loads(_screen("Added")), None)
    after = store.manifest(max_age=3600)
    assert after == from_scratch(store) != first
    await store.write("added", json.loads(_screen("Added, edited")), rev)
    assert store.manifest(max_age=3600) == from_scratch(store) != after
    await store.delete("added")
    assert store.manifest(max_age=3600) == first


def test_the_generation_moves_exactly_when_the_listing_does(roots):
    _, library = roots
    store = _library(library, None)
    entries, generation = store.listing()
    again, same = store.listing()
    assert again is entries and same == generation
    _put(library, "top.screen.json", _screen("Top, renamed"))
    changed, moved = store.listing()
    assert moved != generation and changed != entries
    # The entries are shared; ``manifest`` hands out copies of them.
    store.manifest()["screens"][0]["title"] = "scribbled on"
    assert store.listing()[0] == changed


# --- a failed read ------------------------------------------------------------------------


def test_a_screen_that_fails_to_read_is_not_read_again_while_it_stays_as_it_is(tmp_path, roots, monkeypatch):
    _, library = roots
    store = _library(library, None)
    real, reads = DraftStore.read, []

    def read(self, draft_id):
        reads.append(draft_id)
        if draft_id == "top":
            raise OSError(5, "Input/output error")
        return real(self, draft_id)

    monkeypatch.setattr(DraftStore, "read", read)
    for _ in range(4):
        assert [e["id"] for e in store.manifest()["screens"]] == ["ring/magnets/quad", "ring/overview"]
    assert reads.count("top") == 1
    # A changed file is tried at once.
    _put(library, "top.screen.json", _screen("Top, written again"))
    store.manifest()
    assert reads.count("top") == 2
