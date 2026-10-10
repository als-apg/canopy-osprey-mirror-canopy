"""The listing of a screen library: the pure ``judge`` against the shared vectors, and the thin ``scan``.

``fixtures/listing-vectors.json`` is shared byte for byte with the OSPREY
implementation of the same definition. Its cases are entry lists run through
``judge`` only: this host cannot hold two names that differ only in case.
"""

from __future__ import annotations

import os
import json
import stat
import hashlib
from pathlib import Path

import pytest

from canopy_mcp import listing
from canopy_mcp.store import write_atomic, is_reserved_segment

VECTORS_PATH = Path(__file__).parent / "fixtures" / "listing-vectors.json"
VECTORS_BYTES = VECTORS_PATH.read_bytes()
VECTORS = json.loads(VECTORS_BYTES)
CASES = {case["name"]: case for case in VECTORS["cases"]}

# The non-negated patterns of OSPREY's project ``dockerignore`` that can match below
# ``screens/``, each as a name it matches there.
DOCKERIGNORED = [
    ".env",
    ".env.users",
    ".venv",
    ".git",
    "var",
    "__pycache__",
    "mod.pyc",
    "mod.pyo",
    "mod.pyd",
    ".pytest_cache",
    "build.log",
    "server.pid",
    ".claude",
    "CLAUDE.local.md",
    ".osprey-compose.yml",
    "Dockerfile",
    "prefetched-models",
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _library_list_2c(library: Path) -> bytes:
    """``library_list`` of OSPREY's ``screens_copy.py`` at ``106cdb0dd``, as it stood (the list this one ports)."""
    screens = library / "screens"
    rows: list[tuple[bytes, str]] = []
    for dirpath, _dirnames, filenames in os.walk(screens):
        for name in filenames:
            path = Path(dirpath) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            rel = path.relative_to(screens).as_posix()
            rows.append((rel.encode("utf-8"), f"{_sha(path.read_bytes())}  screens/{rel}\n"))
    return "".join(line for _key, line in sorted(rows)).encode("utf-8")


# --- the file itself ---------------------------------------------------------------------


def test_the_vector_file_is_ascii_only() -> None:
    assert VECTORS_BYTES.isascii()


def test_the_vector_file_has_no_trailing_whitespace() -> None:
    assert b"\r" not in VECTORS_BYTES and b"\t" not in VECTORS_BYTES
    assert [n for n, line in enumerate(VECTORS_BYTES.split(b"\n"), 1) if line != line.rstrip()] == []


def test_the_vector_file_ends_in_exactly_one_newline() -> None:
    assert VECTORS_BYTES.endswith(b"\n") and not VECTORS_BYTES.endswith(b"\n\n")


def test_the_vector_file_holds_the_cases_the_contract_names() -> None:
    paths = {entry["path"] for case in VECTORS["cases"] for entry in case["entries"]}
    assert {
        ".DS_Store",
        "screens/.DS_Store",
        "README.md",
        "screens/prefetched-models/a.screen.json",
        "screens/img.log/a.png",
        "screens/Common/a.screen.json",
        "screens/common/b.screen.json",
        "screens/a/Top.screen.json",
        "screens/a/top.screen.json",
    } <= paths
    codes = {refusal["code"] for case in VECTORS["cases"] for refusal in case["expect"]["refusals"]}
    assert codes == set(listing._REASONS)


# --- judge -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(CASES))
def test_judge_matches_the_vectors(name: str) -> None:
    case = CASES[name]
    judged = listing.judge(case["entries"])
    assert judged.list_bytes.decode("utf-8") == case["expect"]["list"]
    assert judged.digest == case["expect"]["digest"]
    assert list(judged.refusals) == case["expect"]["refusals"]


@pytest.mark.parametrize("name", list(CASES))
def test_judge_does_not_depend_on_the_order_of_entries(name: str) -> None:
    case = CASES[name]
    judged = listing.judge(list(reversed(case["entries"])))
    assert judged.list_bytes.decode("utf-8") == case["expect"]["list"]
    assert list(judged.refusals) == case["expect"]["refusals"]


@pytest.mark.parametrize("name", list(CASES))
def test_the_digest_is_the_sha256_of_the_list(name: str) -> None:
    expect = CASES[name]["expect"]
    assert expect["digest"] == "sha256:" + _sha(expect["list"].encode("utf-8"))
    assert listing.library_digest(expect["list"].encode("utf-8")) == expect["digest"]


def test_a_list_line_is_the_hash_two_spaces_and_the_path() -> None:
    entries = [
        {"path": "screens/b c.png", "kind": "file", "mode": 0o644, "sha256": "2" * 64},
        {"path": "screens/a.screen.json", "kind": "file", "mode": 0o644, "sha256": "1" * 64},
    ]
    judged = listing.judge(entries)
    assert judged.list_bytes == f"{'1' * 64}  screens/a.screen.json\n{'2' * 64}  screens/b c.png\n".encode()
    assert judged.files == (("screens/a.screen.json", "1" * 64), ("screens/b c.png", "2" * 64))
    assert judged.paths == {"screens/a.screen.json", "screens/b c.png"}


def test_the_list_sorts_by_utf8_bytes_not_utf16_units() -> None:
    lines = CASES["the list is sorted by the UTF-8 bytes of the path below screens/"]["expect"]["list"].splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names[-2:] == ["screens/～.png", "screens/\U0001f600.png"]
    assert sorted(names[-2:], key=lambda name: name.encode("utf-16-be")) == list(reversed(names[-2:]))


def test_a_ds_store_file_is_neither_listed_nor_refused() -> None:
    expect = CASES["a regular file named .DS_Store is skipped"]["expect"]
    assert expect["refusals"] == [] and ".DS_Store" not in expect["list"]


def test_a_colliding_pair_names_both_spellings() -> None:
    expect = CASES["a case-only directory pair"]["expect"]
    assert expect["refusals"] == [
        {"path": "screens/Common", "code": "case_collision"},
        {"path": "screens/common", "code": "case_collision"},
    ]


@pytest.mark.parametrize("name", VECTORS["ignoredNames"]["refused"])
def test_ignored_names_are_refused(name: str) -> None:
    assert listing.is_ignored_name(name)


@pytest.mark.parametrize("name", VECTORS["ignoredNames"]["accepted"])
def test_other_names_are_not_ignored(name: str) -> None:
    assert not listing.is_ignored_name(name)


@pytest.mark.parametrize("name", DOCKERIGNORED)
def test_every_dockerignored_name_is_refused_below_screens(name: str) -> None:
    assert listing.is_ignored_name(name)
    judged = listing.judge([{"path": f"screens/a/{name}/x.png", "kind": "file", "mode": 0o644, "sha256": "0" * 64}])
    assert judged.files == ()
    assert [refusal["path"] for refusal in judged.refusals] == [f"screens/a/{name}"]


def test_the_name_predicate_is_the_reserved_rule_without_the_leading_dash() -> None:
    for name in [*VECTORS["ignoredNames"]["refused"], *VECTORS["ignoredNames"]["accepted"]]:
        assert listing.is_ignored_name(name) == (is_reserved_segment(name) and not name.startswith("-")), name


def test_refusal_text_is_one_ascii_line_that_names_the_file() -> None:
    for case in VECTORS["cases"]:
        for refusal in case["expect"]["refusals"]:
            text = listing.refusal_text(refusal)
            assert text.isascii() and "\n" not in text and "\r" not in text
            assert text.startswith(f"{refusal['code']}: ")
            assert "delete" in text
    assert listing.refusal_text({"path": "screens/a\nb.png", "code": "bad_name"}).startswith(
        "bad_name: screens/a\\nb.png: "
    )


# --- scan --------------------------------------------------------------------------------


def _tree(root: Path) -> None:
    (root / "screens" / "a").mkdir(parents=True)
    (root / "screens" / "a" / "top.screen.json").write_bytes(b"{}\n")
    (root / "screens" / "logo.png").write_bytes(b"png")
    (root / "screens" / "a" / "top.screen.json").chmod(0o640)
    (root / "WRITE-BINDINGS.json").write_bytes(b"{}\n")


def test_scan_reports_every_entry_with_kind_mode_and_hash(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "screens" / "link.png").symlink_to("logo.png")
    (tmp_path / "screens" / "dirlink").symlink_to("a", target_is_directory=True)
    os.mkfifo(tmp_path / "screens" / "pipe")
    (tmp_path / "extra" / "deep").mkdir(parents=True)
    (tmp_path / "extra" / "deep" / "x.png").write_bytes(b"x")

    entries = listing.scan(tmp_path)
    by_path = {entry["path"]: entry for entry in entries}
    assert [entry["path"] for entry in entries] == [
        "WRITE-BINDINGS.json",
        "extra",
        "screens",
        "screens/a",
        "screens/a/top.screen.json",
        "screens/dirlink",
        "screens/link.png",
        "screens/logo.png",
        "screens/pipe",
    ]
    assert {path: entry["kind"] for path, entry in by_path.items()} == {
        "WRITE-BINDINGS.json": "file",
        "extra": "dir",
        "screens": "dir",
        "screens/a": "dir",
        "screens/a/top.screen.json": "file",
        "screens/dirlink": "symlink",
        "screens/link.png": "symlink",
        "screens/logo.png": "file",
        "screens/pipe": "other",
    }
    assert by_path["screens/a/top.screen.json"] == {
        "path": "screens/a/top.screen.json",
        "kind": "file",
        "mode": 0o640,
        "sha256": _sha(b"{}\n"),
    }
    assert [path for path, entry in by_path.items() if "sha256" in entry] == [
        "WRITE-BINDINGS.json",
        "screens/a/top.screen.json",
        "screens/logo.png",
    ]
    assert all(isinstance(entry["mode"], int) for entry in entries)


def test_scan_of_a_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        listing.scan(tmp_path / "absent")


def test_scan_then_judge_equals_the_ported_list_on_a_clean_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "screens" / "a" / "b c").mkdir()
    (tmp_path / "screens" / "a" / "b c" / "Z.PNG").write_bytes(b"z")
    judged = listing.judge(listing.scan(tmp_path))
    assert judged.refusals == ()
    assert judged.list_bytes == _library_list_2c(tmp_path)
    assert judged.digest == "sha256:" + _sha(_library_list_2c(tmp_path))


def test_an_interrupted_write_leaves_what_the_listing_refuses(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path)
    target = tmp_path / "screens" / "a" / "new.screen.json"

    def fail(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", fail)
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: None)  # as a killed process would leave it
    with pytest.raises(KeyboardInterrupt):
        write_atomic(target, b"{}\n")
    monkeypatch.undo()
    refusals = listing.judge(listing.scan(tmp_path)).refusals
    assert [refusal["code"] for refusal in refusals] == ["leftover_tmp"]
    assert refusals[0]["path"].startswith("screens/a/.new.screen.json.")
