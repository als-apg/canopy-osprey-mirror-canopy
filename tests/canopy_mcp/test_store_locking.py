"""The stores' cross-process safety: the directory flock, fd-keyed revs, read-only refusals and modes."""

import os
import sys
import errno
import fcntl
import asyncio
import threading
import subprocess

import pytest

from canopy_mcp import store as store_mod
from canopy_mcp import views as views_mod
from canopy_mcp.store import UMASK, DraftStore, RevConflict, ReadOnlyStore, lock_root
from canopy_mcp.views import ViewStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _doc(title: str) -> dict:
    return {"metadata": {"title": title}, "root": {"type": "group", "children": []}}


@pytest.fixture
def store(tmp_path):
    return DraftStore(tmp_path / "drafts")


def _is_locked(path) -> bool:
    """Whether another descriptor holds an flock on the directory ``path``."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _files(root) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class _Gate:
    """Parks the first ``write_atomic`` call of ``module`` inside its locked section until released."""

    def __init__(self, monkeypatch, module):
        self.entered = threading.Event()
        self.release = threading.Event()
        real = module.write_atomic

        def gated(path, data):
            if not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(30), "the test never released the parked write"
            return real(path, data)

        monkeypatch.setattr(module, "write_atomic", gated)

    async def wait_entered(self):
        assert await asyncio.to_thread(self.entered.wait, 30)


# --- lock_root ----------------------------------------------------------------


def test_lock_root_excludes_a_second_descriptor_and_frees_on_exit(tmp_path):
    assert not _is_locked(tmp_path)
    with lock_root(tmp_path) as held:
        assert held is None
        assert _is_locked(tmp_path)
    assert not _is_locked(tmp_path)


def test_lock_root_frees_the_lock_when_the_block_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with lock_root(tmp_path):
            raise RuntimeError("boom")
    assert not _is_locked(tmp_path)


def test_lock_root_shared_admits_readers_and_excludes_a_writer(tmp_path):
    with lock_root(tmp_path, exclusive=False):
        with lock_root(tmp_path, exclusive=False):  # would deadlock if it were exclusive
            assert _is_locked(tmp_path)


def test_lock_root_leaves_no_file_behind(tmp_path):
    with lock_root(tmp_path):
        assert list(tmp_path.iterdir()) == []
    assert list(tmp_path.iterdir()) == []


def test_lock_root_needs_an_existing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        with lock_root(tmp_path / "absent"):
            pass
    assert not (tmp_path / "absent").exists()
    (tmp_path / "file").write_text("x")
    with pytest.raises(NotADirectoryError):
        with lock_root(tmp_path / "file"):
            pass


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_lock_root_locks_a_read_only_directory(tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o555)
    try:
        with lock_root(root, exclusive=False):
            with lock_root(root, exclusive=False):
                pass
        with lock_root(root):
            assert _is_locked(root)
    finally:
        root.chmod(0o755)


# --- two processes ------------------------------------------------------------

_CHILD = """
import sys, asyncio
from canopy_mcp import store as store_mod
from canopy_mcp.store import DraftStore, RevConflict

root, title, park = sys.argv[1], sys.argv[2], sys.argv[3] == "park"
if park:
    real = store_mod.write_atomic

    def parked(path, data):
        print("in-section", flush=True)
        sys.stdin.readline()
        return real(path, data)

    store_mod.write_atomic = parked

async def main():
    doc = {"metadata": {"title": title}, "root": {"type": "group", "children": []}}
    print("started", flush=True)
    try:
        await DraftStore(root).write("a", doc, None)
    except RevConflict:
        print("conflict", flush=True)
    else:
        print("ok", flush=True)

asyncio.run(main())
"""


def _child(root, title, mode):
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(root), title, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_creating_one_draft_give_exactly_one_rev_conflict(tmp_path):
    root = tmp_path / "drafts"
    first = _child(root, "first", "park")
    second = None
    try:
        # The first process has verified "no such draft" and sits inside its locked section.
        assert first.stdout.readline().strip() == "started", first.stderr.read()
        assert first.stdout.readline().strip() == "in-section", first.stderr.read()
        assert _is_locked(root)
        second = _child(root, "second", "plain")
        assert second.stdout.readline().strip() == "started", second.stderr.read()
        # Without the directory lock the second process would create the draft right here.
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=1.0)
        assert _files(root) == []
        first.stdin.write("go\n")
        first.stdin.flush()
        out_first, err_first = first.communicate(timeout=30)
        out_second, err_second = second.communicate(timeout=30)
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
    assert (first.returncode, second.returncode) == (0, 0), err_first + err_second
    results = sorted([out_first.strip(), out_second.strip()])
    assert results == ["conflict", "ok"]
    assert out_first.strip() == "ok"
    assert DraftStore(root).read("a")[0]["metadata"]["title"] == "first"
    assert _files(root) == ["screens/a.screen.json"]


async def test_two_store_instances_in_one_process_exclude_each_other(tmp_path, monkeypatch):
    # Separate instances share no asyncio.Lock, so only the directory flock orders them.
    one, two = DraftStore(tmp_path / "drafts"), DraftStore(tmp_path / "drafts")
    gate = _Gate(monkeypatch, store_mod)
    first = asyncio.create_task(one.write("a", _doc("one"), None))
    try:
        await gate.wait_entered()
        second = asyncio.create_task(two.write("a", _doc("two"), None))
        await asyncio.sleep(0.2)
        assert not second.done()
    finally:
        gate.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], str)
    assert isinstance(results[1], RevConflict)
    assert one.read("a")[0]["metadata"]["title"] == "one"


# --- reads key the cache from the fd they hashed --------------------------------


def _swap_on_digest(monkeypatch, path, replacement: bytes):
    """Replace ``path`` with same-size ``replacement`` the first time the store hashes."""
    real = store_mod._digest
    state = {"done": False}

    def digest(data):
        if not state["done"]:
            state["done"] = True
            assert len(replacement) == path.stat().st_size
            other = path.with_name("swap.tmp")
            other.write_bytes(replacement)
            os.replace(other, path)
        return real(data)

    monkeypatch.setattr(store_mod, "_digest", digest)
    return real


async def test_read_detects_a_same_size_replace_between_hash_and_stat(store, monkeypatch):
    old_rev = await store.write("a", _doc("aaaa"), None)
    path = store.path_for("a")
    replacement = path.read_bytes().replace(b"aaaa", b"bbbb")
    fresh = DraftStore(store.root)
    real_digest = _swap_on_digest(monkeypatch, path, replacement)
    doc, rev = fresh.read("a")
    # The read describes the bytes it parsed ...
    assert (doc["metadata"]["title"], rev) == ("aaaa", old_rev)
    # ... and did not pin that rev to the file that replaced them.
    assert fresh.rev("a") == real_digest(replacement) != old_rev
    with pytest.raises(RevConflict):
        await fresh.write("a", _doc("cccc"), old_rev)
    assert path.read_bytes() == replacement


async def test_rev_detects_a_same_size_replace_while_it_hashes(store, monkeypatch):
    old_rev = await store.write("a", _doc("aaaa"), None)
    path = store.path_for("a")
    replacement = path.read_bytes().replace(b"aaaa", b"bbbb")
    fresh = DraftStore(store.root)
    real_digest = _swap_on_digest(monkeypatch, path, replacement)
    assert fresh.rev("a") == old_rev
    assert fresh.rev("a") == real_digest(replacement)


async def test_a_same_size_replace_between_the_read_and_the_stat_is_detected(store, monkeypatch):
    old_rev = await store.write("a", _doc("aaaa"), None)
    path = store.path_for("a")
    replacement = path.read_bytes().replace(b"aaaa", b"bbbb")
    fresh = DraftStore(store.root)
    real = os.fstat
    calls = []

    def fstat(fd):
        calls.append(fd)
        if len(calls) == 2:  # the stat that follows the read
            other = path.with_name("swap.tmp")
            other.write_bytes(replacement)
            os.replace(other, path)
        return real(fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_mod.os, "fstat", fstat)
        doc, rev = fresh.read("a")
    assert len(calls) == 2, "the read no longer stats the descriptor it hashed"
    assert (doc["metadata"]["title"], rev) == ("aaaa", old_rev)
    assert fresh.rev("a") == store_mod._digest(replacement) != old_rev
    with pytest.raises(RevConflict):
        await fresh.write("a", _doc("cccc"), old_rev)


async def test_a_file_edited_in_place_during_the_read_is_not_cached(store, monkeypatch):
    await store.write("a", _doc("aaaa"), None)
    path = store.path_for("a")
    real = store_mod._read_keyed

    def read_then_touch(target):
        data, key = real(target)
        return data, None  # what ``_read_keyed`` answers when the two fstats disagree

    fresh = DraftStore(store.root)
    monkeypatch.setattr(store_mod, "_read_keyed", read_then_touch)
    fresh.read("a")
    fresh.rev("a")
    assert path not in fresh._revs


def test_read_keyed_withholds_the_key_when_the_file_changes_under_it(tmp_path, monkeypatch):
    path = tmp_path / "f"
    path.write_bytes(b"one")
    real = os.fstat
    calls = []

    def fstat(fd):
        calls.append(fd)
        if len(calls) == 2:  # the stat after the read: the file grew meanwhile
            with open(path, "ab") as fh:
                fh.write(b"more")
        return real(fd)

    monkeypatch.setattr(store_mod.os, "fstat", fstat)
    data, key = store_mod._read_keyed(path)
    monkeypatch.undo()
    assert (data, key) == (b"one", None)
    data, key = store_mod._read_keyed(path)
    st = path.stat()
    assert (data, key) == (b"onemore", (st.st_ino, st.st_mtime_ns, st.st_size))


async def test_an_unchanged_draft_answers_rev_without_opening_the_file(store, monkeypatch):
    rev = await store.write("a", _doc("a"), None)

    def no_open(path):
        raise AssertionError("rev re-read an unchanged file")

    monkeypatch.setattr(store_mod, "_read_keyed", no_open)
    assert store.rev("a") == rev


# --- roots: created, refused, read-only ---------------------------------------


async def test_a_nonexistent_root_is_created_with_umask_modes(tmp_path):
    root = tmp_path / "deep" / "drafts"
    store = DraftStore(root)
    assert not root.exists()  # constructing touches nothing
    await store.write("pkg/a", _doc("a"), None)
    await store.write_asset("img/logo.png", PNG)
    for directory in (root, root / "screens", root / "screens" / "pkg", root / "screens" / "img"):
        assert directory.stat().st_mode & 0o777 == 0o777 & ~UMASK, directory
    for file in (store.path_for("pkg/a"), root / "screens" / "img" / "logo.png"):
        assert file.stat().st_mode & 0o777 == 0o666 & ~UMASK, file
    assert _files(root) == ["screens/img/logo.png", "screens/pkg/a.screen.json"]


def test_umask_is_read_without_changing_it():
    current = os.umask(0)
    os.umask(current)
    assert UMASK == current
    assert (store_mod.FILE_MODE, store_mod.DIR_MODE) == (0o666 & ~UMASK, 0o777 & ~UMASK)


async def test_a_replaced_draft_keeps_the_umask_mode(store):
    rev = await store.write("a", _doc("one"), None)
    await store.write("a", _doc("two"), rev)
    assert store.path_for("a").stat().st_mode & 0o777 == 0o666 & ~UMASK


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
async def test_an_existing_0555_root_is_refused(tmp_path):
    root = tmp_path / "drafts"
    root.mkdir()
    root.chmod(0o555)
    store = DraftStore(root)
    try:
        with pytest.raises(ReadOnlyStore) as write:
            await store.write("a", _doc("a"), None)
        with pytest.raises(ReadOnlyStore):
            await store.write_asset("logo.png", PNG)
        assert list(root.iterdir()) == []
        assert not _is_locked(root)
    finally:
        root.chmod(0o755)
    assert str(write.value).startswith("read_only_store")
    assert not isinstance(write.value, OSError)
    assert isinstance(write.value.__cause__, PermissionError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
async def test_a_read_only_tree_refuses_replace_and_delete_and_still_reads(store):
    rev = await store.write("a", _doc("a"), None)
    store.screens.chmod(0o555)
    try:
        with pytest.raises(ReadOnlyStore):
            await store.write("a", _doc("b"), rev)
        with pytest.raises(ReadOnlyStore):
            await store.delete("a", rev)
        with pytest.raises(ReadOnlyStore):
            await store.write("new", _doc("n"), None)
        assert store.read("a") == (_doc("a"), rev)
        assert store.rev("a") == rev
        assert [s["id"] for s in store.manifest()["screens"]] == ["a"]
    finally:
        store.screens.chmod(0o755)
    assert _files(store.root) == ["screens/a.screen.json"]


async def test_a_read_only_store_refuses_every_mutation_before_touching_disk(tmp_path, monkeypatch):
    writable = DraftStore(tmp_path / "library")
    rev = await writable.write("a", _doc("a"), None)
    await writable.write_asset("logo.png", PNG)
    before = _files(tmp_path)
    store = DraftStore(tmp_path / "library", read_only=True)

    def no_thread(*args, **kwargs):
        raise AssertionError("a read-only store started a mutation section")

    monkeypatch.setattr(store_mod.asyncio, "to_thread", no_thread)
    for mutation in (
        store.write("a", _doc("b"), rev),
        store.write("new", _doc("n"), None),
        store.write_many({"a": (_doc("b"), rev)}),
        store.write_asset("logo.png", PNG, overwrite=True),
        store.write_asset("../escape.png", PNG),
        store.delete("a", rev),
        store.delete("a"),
    ):
        with pytest.raises(ReadOnlyStore, match=r"^read_only_store: "):
            await mutation
    assert _files(tmp_path) == before
    assert store.read("a") == (_doc("a"), rev)
    assert store.rev("a") == rev
    assert store.list() == ["a"]
    assert store.resolve_static("screens/logo.png") is not None


async def test_a_read_only_store_over_an_absent_root_creates_nothing(tmp_path):
    store = DraftStore(tmp_path / "absent", read_only=True)
    with pytest.raises(ReadOnlyStore):
        await store.write("a", _doc("a"), None)
    assert not (tmp_path / "absent").exists()


def test_read_only_store_is_not_an_oserror():
    assert not issubclass(ReadOnlyStore, OSError)
    assert str(ReadOnlyStore("x")) == "read_only_store: x"
    assert ReadOnlyStore.code == "read_only_store"


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("call", ["mkdir", "mkstemp", "replace", "unlink"])
async def test_refusing_errnos_become_read_only_store(store, monkeypatch, call, code):
    rev = await store.write("a", _doc("a"), None)

    def refuse(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    target = {
        "mkdir": (store_mod.Path, "mkdir"),
        "mkstemp": (store_mod.tempfile, "mkstemp"),
        "replace": (store_mod.os, "replace"),
        "unlink": (store_mod.os, "unlink"),
    }[call]
    with monkeypatch.context() as patch:
        patch.setattr(*target, refuse)
        with pytest.raises(ReadOnlyStore) as caught:
            if call == "unlink":
                await store.delete("a", rev)
            else:
                await store.write("a", _doc("b"), rev)
        assert caught.value.__cause__.errno == code
    assert not _is_locked(store.root)
    assert _files(store.root) == ["screens/a.screen.json"]
    assert store.read("a") == (_doc("a"), rev)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, None])
async def test_other_write_errors_stay_oserror(store, monkeypatch, code):
    rev = await store.write("a", _doc("a"), None)

    def fail(*args, **kwargs):
        raise OSError("disk full") if code is None else OSError(code, os.strerror(code))

    with monkeypatch.context() as patch:
        patch.setattr(store_mod.os, "replace", fail)
        with pytest.raises(OSError):
            await store.write("a", _doc("b"), rev)
    assert _files(store.root) == ["screens/a.screen.json"]
    assert store.rev("a") == rev


async def test_deleting_from_a_store_without_a_root_creates_nothing(store):
    from canopy_mcp.store import DraftNotFound

    with pytest.raises(DraftNotFound):
        await store.delete("a")
    assert not store.root.exists()


# --- cancellation ---------------------------------------------------------------


async def test_a_cancelled_mutation_leaves_the_lock_free(store, monkeypatch):
    rev = await store.write("a", _doc("one"), None)
    gate = _Gate(monkeypatch, store_mod)
    task = asyncio.create_task(store.write("a", _doc("two"), rev))
    try:
        await gate.wait_entered()
        assert _is_locked(store.root)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The asyncio.Lock went with the task; the section itself runs on in its thread ...
        assert not store._lock.locked()
        follower = asyncio.create_task(store.write("b", _doc("b"), None))
        await asyncio.sleep(0.2)
        # ... so the next mutation waits on the directory lock instead of interleaving.
        assert not follower.done()
        assert not store.path_for("b").exists()
    finally:
        gate.release.set()
    assert isinstance(await asyncio.wait_for(follower, 30), str)
    assert not _is_locked(store.root)
    assert not store._lock.locked()
    # The cancelled section ran to its end: nothing half-written, nothing left behind.
    assert store.read("a")[0]["metadata"]["title"] == "two"
    assert _files(store.root) == ["screens/a.screen.json", "screens/b.screen.json"]


async def test_a_mutation_cancelled_while_queued_never_runs(store):
    async with store._lock:
        task = asyncio.create_task(store.write("a", _doc("a"), None))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not store._lock.locked()
    assert not store.root.exists()
    assert await store.write("a", _doc("a"), None)


async def test_a_failed_mutation_leaves_the_lock_free(store):
    with pytest.raises(RevConflict):
        await store.write("a", _doc("a"), "0" * 64)
    assert not _is_locked(store.root)
    assert not store._lock.locked()
    assert _files(store.root) == []


# --- views ----------------------------------------------------------------------


async def test_two_concurrent_view_puts_serialise_with_views_dir_outside_the_root(tmp_path, monkeypatch):
    drafts, saved = tmp_path / "drafts", tmp_path / "volume" / "saved"
    # Two stores, as two processes would hold: no shared asyncio.Lock.
    one, two = ViewStore(drafts, views_dir=saved), ViewStore(drafts, views_dir=saved)
    gate = _Gate(monkeypatch, views_mod)
    first = asyncio.create_task(one.put("orbit", "One", "pv=A"))
    try:
        await gate.wait_entered()
        # The lock is on the named directory, and the drafts root is neither locked nor created.
        assert _is_locked(saved)
        assert not drafts.exists()
        second = asyncio.create_task(two.put("orbit", "Two", "pv=B"))
        await asyncio.sleep(0.2)
        assert not second.done()
        assert _files(tmp_path) == []
    finally:
        gate.release.set()
    (_, created_one), (view_two, created_two) = await asyncio.gather(first, second)
    assert (created_one, created_two) == (True, False)
    assert one.get("orbit") == view_two
    assert view_two["name"] == "Two"
    assert _files(tmp_path) == ["volume/saved/orbit.json"]
    assert not drafts.exists()
    assert not _is_locked(saved)


async def test_a_kind_change_racing_a_put_is_judged_under_the_lock(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    one, two = ViewStore(tmp_path / "drafts", views_dir=saved), ViewStore(tmp_path / "drafts", views_dir=saved)
    gate = _Gate(monkeypatch, views_mod)
    first = asyncio.create_task(one.put("v", "One", "pv=A", "trace"))
    try:
        await gate.wait_entered()
        second = asyncio.create_task(two.put("v", "Two", "pv=B", "logbook"))
        await asyncio.sleep(0.2)
        assert not second.done()
    finally:
        gate.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[1], views_mod.ViewKindConflict)
    assert one.get("v")["kind"] == "trace"


async def test_view_files_and_directories_get_umask_modes(tmp_path):
    store = ViewStore(tmp_path / "drafts")
    await store.put("v", "V", "pv=A")
    assert store.views.stat().st_mode & 0o777 == 0o777 & ~UMASK
    assert store.path_for("v").stat().st_mode & 0o777 == 0o666 & ~UMASK


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
async def test_a_0555_views_directory_refuses_put_and_delete(tmp_path):
    store = ViewStore(tmp_path / "drafts")
    await store.put("v", "V", "pv=A")
    store.views.chmod(0o555)
    try:
        with pytest.raises(ReadOnlyStore):
            await store.put("v", "W", "pv=B")
        with pytest.raises(ReadOnlyStore):
            await store.put("new", "N", "pv=C")
        with pytest.raises(ReadOnlyStore):
            await store.delete("v")
        with pytest.raises(views_mod.ViewNotFound):
            await store.delete("absent")
        assert store.get("v")["name"] == "V"
        assert not _is_locked(store.views)
    finally:
        store.views.chmod(0o755)
    assert _files(tmp_path) == ["drafts/views/v.json"]
