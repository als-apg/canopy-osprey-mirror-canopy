"""``request_push``: a bundle cut from one state of the store, judged without a lock, written under another.

The tool reads the push closure under the store's ``flock``, releases it, judges
the bytes it read (closure, grammar, check) and writes exactly those bytes into
``CANOPY_PUSH_DIR`` under that directory's ``flock``. These tests pin the
bundle format, every refusal, the two locked sections and the pruning.
"""

import os
import re
import json
import fcntl
import shutil
import asyncio
import hashlib
import threading
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

from canopy_mcp.store import MAX_ASSET_BYTES, ReadOnlyStore
from canopy_mcp.tools import push, _common
from canopy_mcp.config import Settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.check.lint import RACCOON_VALIDATE_PATH
from canopy_mcp.check.refs import StoreTree
from canopy_mcp.tools.push import request_push

BASE = "http://canopy.test"
RACCOON = BASE + RACCOON_VALIDATE_PATH
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
RID = re.compile(r"[0-9a-f]{16}")
# The deployment's own reserved names, as CANOPY_LIBRARY_IGNORED_NAMES gives them.
IGNORED_NAMES = "var Dockerfile prefetched-models CLAUDE.local.md"
IGNORED = frozenset(IGNORED_NAMES.casefold().split())
REQUEST_KEYS = ["format", "rid", "source_id", "source_dir", "rev", "target_dir", "note", "created", "files"]


def _catalog():
    types = ("group", "label", "pv-label", "embedded-display", "image-view", "action-button")
    return build_catalog({t: {"consumes": [], "verbatim": False} for t in types})


def _node(nid, ntype="label", props=None, children=None, **extra):
    node = {"id": nid, "type": ntype, "props": props or {}, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}
    if children is not None:
        node["children"] = children
    node.update(extra)
    return node


def _doc(*children, title="t"):
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": title, "source": {"type": "native"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": _node("root", "group", children=list(children)),
    }


def _embed(nid, file):
    return _node(nid, "embedded-display", {"file": file})


def _image(nid, src):
    return _node(nid, "image-view", {"src": src})


def _bytes(doc) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode()


def _put(settings: Settings, files: dict) -> dict[str, bytes]:
    """Write ``{path below screens/: document or bytes}`` into the drafts tree; returns the bytes written."""
    written = {}
    for path, content in files.items():
        data = content if isinstance(content, bytes) else _bytes(content)
        target = Path(settings.drafts_dir) / "screens" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written[path] = data
    return written


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


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


def _only_bundle(settings: Settings) -> Path:
    (bundle,) = [p for p in Path(settings.push_dir).iterdir() if p.is_dir()]
    return bundle


def _settings(tmp_path, monkeypatch, **overrides) -> Settings:
    monkeypatch.setenv("CANOPY_LIBRARY_IGNORED_NAMES", IGNORED_NAMES)
    s = Settings(base_url=BASE, drafts_dir=tmp_path / "drafts", **overrides)
    monkeypatch.setattr(push, "get_settings", lambda: s)
    return s


@pytest.fixture(autouse=True)
def clean_state():
    _common.reset_stores()
    set_catalog(_catalog())
    yield
    set_catalog(None)
    _common.reset_stores()


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    return _settings(tmp_path, monkeypatch, push_dir=tmp_path / "push")


@pytest.fixture
def view(settings) -> dict[str, bytes]:
    """``rf/view`` embedding ``rf/parts/inner`` and showing ``rf/img/logo.png``."""
    return _put(
        settings,
        {
            "rf/view.screen.json": _doc(_embed("e", "parts/inner.screen.json"), _image("i", "img/logo.png")),
            "rf/parts/inner.screen.json": _doc(_node("l", props={"text": "inner"})),
            "rf/img/logo.png": PNG,
            "rf/unrelated.screen.json": _doc(),
        },
    )


# --- the setting ----------------------------------------------------------------------


def test_push_dir_is_unset_by_default_and_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CANOPY_PUSH_DIR", raising=False)
    assert Settings().push_dir is None
    monkeypatch.setenv("CANOPY_PUSH_DIR", "")
    assert Settings().push_dir is None
    monkeypatch.setenv("CANOPY_PUSH_DIR", str(tmp_path / "push"))
    assert Settings().push_dir == tmp_path / "push"


async def test_without_a_push_dir_the_tool_answers_push_unavailable(tmp_path, monkeypatch) -> None:
    s = _settings(tmp_path, monkeypatch)
    _put(s, {"view.screen.json": _doc()})
    before = _tree(tmp_path)

    result = await request_push("view", "sr/rf", "a note")

    assert (result["draftId"], result["refused"], result["reason"]) == ("view", True, "push_unavailable")
    assert result["message"].startswith("push_unavailable: ")
    assert _tree(tmp_path) == before


async def test_an_empty_push_dir_variable_answers_push_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CANOPY_PUSH_DIR", "")
    s = Settings(base_url=BASE, drafts_dir=tmp_path / "drafts")
    monkeypatch.setattr(push, "get_settings", lambda: s)
    _put(s, {"view.screen.json": _doc()})

    assert (await request_push("view", "sr/rf", "a note"))["reason"] == "push_unavailable"


# --- the bundle -----------------------------------------------------------------------


async def test_a_push_writes_the_closure_and_a_request_that_describes_it(settings, view) -> None:
    result = await request_push("rf/view", "sr/rf", "RF overview, used every shift")

    members = ["img/logo.png", "parts/inner.screen.json", "view.screen.json"]
    rev = hashlib.sha256(view["rf/view.screen.json"]).hexdigest()
    assert RID.fullmatch(result["rid"])
    assert result == {
        "draftId": "rf/view",
        "rid": result["rid"],
        "rev": rev,
        "sourceDir": "rf",
        "targetDir": "sr/rf",
        "files": members,
    }
    bundle = Path(settings.push_dir) / result["rid"]
    assert _tree(bundle) == [
        "files",
        "files/img",
        "files/img/logo.png",
        "files/parts",
        "files/parts/inner.screen.json",
        "files/view.screen.json",
        "request.json",
    ]
    for member in members:
        assert (bundle / "files" / member).read_bytes() == view[f"rf/{member}"]

    raw = (bundle / "request.json").read_bytes()
    request = json.loads(raw)
    assert raw.isascii() and raw.endswith(b"\n")
    assert list(request) == REQUEST_KEYS
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", request.pop("created"))
    assert request == {
        "format": "canopy-push/1",
        "rid": result["rid"],
        "source_id": "rf/view",
        "source_dir": "rf",
        "rev": rev,
        "target_dir": "sr/rf",
        "note": "RF overview, used every shift",
        "files": [
            {"path": m, "size": len(view[f"rf/{m}"]), "sha256": hashlib.sha256(view[f"rf/{m}"]).hexdigest()}
            for m in members
        ],
    }
    # The store is only read, and both locks are released.
    assert not _is_locked(settings.drafts_dir) and not _is_locked(settings.push_dir)
    assert _tree(Path(settings.drafts_dir) / "screens" / "rf") == [
        "img",
        "img/logo.png",
        "parts",
        "parts/inner.screen.json",
        "unrelated.screen.json",
        "view.screen.json",
    ]


async def test_a_top_level_screen_is_pushed_from_the_top_level(settings) -> None:
    _put(settings, {"view.screen.json": _doc()})

    result = await request_push("view", "sr", "note")

    assert (result["sourceDir"], result["files"]) == ("", ["view.screen.json"])
    assert json.loads((_only_bundle(settings) / "request.json").read_bytes())["source_dir"] == ""


async def test_a_wider_source_dir_carries_what_the_screen_reaches_beside_its_own_directory(settings) -> None:
    _put(
        settings,
        {
            "sr/rf/view.screen.json": _doc(_embed("e", "../common/knob.screen.json")),
            "sr/common/knob.screen.json": _doc(),
        },
    )

    narrow = await request_push("sr/rf/view", "ring", "note")
    wide = await request_push("sr/rf/view", "ring", "note", source_dir="sr")

    assert (narrow["reason"], [r["code"] for r in narrow["refusals"]]) == ("closure_refused", ["ref_leaves_source"])
    assert wide["files"] == ["common/knob.screen.json", "rf/view.screen.json"]
    assert (Path(settings.push_dir) / wide["rid"] / "files" / "rf" / "view.screen.json").is_file()


async def test_a_note_may_hold_any_plain_text_and_is_stored_as_ascii_json(settings) -> None:
    _put(settings, {"view.screen.json": _doc()})
    note = "Überblick über den Speicherring\n“quoted”\tand tabbed"

    await request_push("view", "sr", note)

    raw = (_only_bundle(settings) / "request.json").read_bytes()
    assert raw.isascii() and json.loads(raw)["note"] == note


# --- arguments ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("draft_id", "source_dir", "match"),
    [
        ("rf/view", "", "only for a top-level id"),
        ("rf/view", "other", "does not contain"),
        ("rf/view", "r", "does not contain"),
        ("rf/view", "rf/view", "does not contain"),
        ("rf/view", "rf/", "invalid source_dir"),
        ("rf/view", "../rf", "invalid source_dir"),
        ("rf/view", "/rf", "invalid source_dir"),
    ],
)
async def test_a_source_dir_that_does_not_hold_the_screen_is_a_bad_argument(
    settings, view, draft_id: str, source_dir: str, match: str
) -> None:
    with pytest.raises(ToolError, match=match):
        await request_push(draft_id, "sr/rf", "note", source_dir=source_dir)
    assert not Path(settings.push_dir).exists()


@pytest.mark.parametrize(
    ("target_dir", "match"),
    [
        ("library", "may not start with the segment 'library'"),
        ("library/sr", "may not start with the segment 'library'"),
        ("", "invalid target_dir"),
        ("../x", "invalid target_dir"),
        ("/sr", "invalid target_dir"),
        ("sr/", "invalid target_dir"),
        ("sr/a.b", "invalid target_dir"),
        ("sr/a b", "invalid target_dir"),
        ("var", "reserved segment 'var'"),
        ("sr/VAR", "reserved segment 'VAR'"),
        ("sr/__pycache__", "reserved segment '__pycache__'"),
        ("sr/-x", "reserved segment '-x'"),
        ("sr/Dockerfile", "reserved segment 'Dockerfile'"),
        ("prefetched-models/a", "reserved segment 'prefetched-models'"),
    ],
)
async def test_a_target_dir_outside_the_grammar_is_a_bad_argument(settings, view, target_dir: str, match: str) -> None:
    with pytest.raises(ToolError, match=match):
        await request_push("rf/view", target_dir, "note")
    assert not Path(settings.push_dir).exists()


async def test_a_target_dir_may_hold_library_below_its_first_segment(settings, view) -> None:
    assert (await request_push("rf/view", "sr/library", "note"))["targetDir"] == "sr/library"


async def test_the_deployment_names_are_free_when_none_are_configured(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch, push_dir=tmp_path / "push", library_ignored_names="")
    _put(
        settings,
        {"rf/view.screen.json": _doc(_embed("e", "var/inner.screen.json")), "rf/var/inner.screen.json": _doc()},
    )

    result = await request_push("rf/view", "sr/Dockerfile", "note")

    assert result["targetDir"] == "sr/Dockerfile"
    assert result["files"] == ["var/inner.screen.json", "view.screen.json"]


@pytest.mark.parametrize(
    ("note", "match"),
    [
        ("a\x1b[2Jb", "control, format or bidi"),
        ("a\u202eb", "control, format or bidi"),
        ("a\x00b", "control, format or bidi"),
        ("x" * 2001, "at most 2000"),
    ],
)
async def test_a_note_with_a_forbidden_character_or_too_long_is_a_bad_argument(
    settings, view, note: str, match: str
) -> None:
    with pytest.raises(ToolError, match=match):
        await request_push("rf/view", "sr/rf", note)
    assert not Path(settings.push_dir).exists()


@pytest.mark.parametrize("draft_id", ["rf/absent", "absent"])
async def test_an_id_with_no_stored_draft_is_a_bad_argument(settings, view, draft_id: str) -> None:
    with pytest.raises(ToolError, match="no draft stored under"):
        await request_push(draft_id, "sr/rf", "note")
    assert not Path(settings.push_dir).exists()


async def test_a_store_that_does_not_exist_yet_has_no_draft(settings) -> None:
    with pytest.raises(ToolError, match="no draft stored under 'view'"):
        await request_push("view", "sr", "note")
    assert not Path(settings.drafts_dir).exists()


@pytest.mark.parametrize("draft_id", ["../view", "rf/view.screen.json", "", "rf//view"])
async def test_an_invalid_id_is_a_bad_argument(settings, view, draft_id: str) -> None:
    with pytest.raises(ToolError, match="invalid draft id"):
        await request_push(draft_id, "sr/rf", "note")


# --- the source is not the library ----------------------------------------------------


async def test_a_library_screen_is_refused_before_anything_is_read(tmp_path, monkeypatch) -> None:
    library = tmp_path / "library"
    (library / "screens" / "sr").mkdir(parents=True)
    (library / "screens" / "sr" / "overview.screen.json").write_bytes(_bytes(_doc()))
    _settings(tmp_path, monkeypatch, push_dir=tmp_path / "push", library_dir=library)

    result = await request_push("library/sr/overview", "sr", "note")

    assert (result["draftId"], result["refused"], result["reason"]) == ("library/sr/overview", True, "read_only_store")
    assert not (tmp_path / "push").exists()


async def test_without_a_library_a_draft_under_library_is_still_not_pushed(settings) -> None:
    _put(settings, {"library/view.screen.json": _doc()})

    result = await request_push("library/view", "sr", "note")

    assert (result["refused"], result["reason"]) == (True, "library_source")
    assert not Path(settings.push_dir).exists()


# --- closure refusals -----------------------------------------------------------------


async def test_a_closure_refusal_lists_every_ref_and_files_nothing(settings) -> None:
    _put(
        settings,
        {
            "rf/view.screen.json": _doc(
                _embed("gone", "parts/absent.screen.json"),
                _embed("up", "../other/x.screen.json"),
                _embed("macro", "$(P)/x.screen.json"),
            ),
            "other/x.screen.json": _doc(),
        },
    )

    result = await request_push("rf/view", "sr/rf", "note")

    assert (result["refused"], result["reason"]) == (True, "closure_refused")
    assert [(r["code"], r["nodeId"]) for r in result["refusals"]] == [
        ("unresolved_ref", "gone"),
        ("ref_leaves_source", "up"),
        ("macro_ref", "macro"),
    ]
    assert {"doc", "ref", "reason", "message"} <= set(result["refusals"][0])
    assert not Path(settings.push_dir).exists()


# --- grammar refusals -----------------------------------------------------------------


async def _grammar_refusals(settings, files: dict, top: str = "rf/view") -> list[dict]:
    _put(settings, files)
    result = await request_push(top, "sr/rf", "note")
    assert (result["refused"], result["reason"]) == (True, "grammar_refused"), result
    assert not Path(settings.push_dir).exists()
    return result["refusals"]


@pytest.mark.parametrize("segment", ["var", "VAR", "__pycache__", "-parts", "dockerfile", "prefetched-models"])
async def test_a_screen_under_a_reserved_segment_is_refused(settings, segment: str) -> None:
    refusals = await _grammar_refusals(
        settings,
        {
            "rf/view.screen.json": _doc(_embed("e", f"{segment}/inner.screen.json")),
            f"rf/{segment}/inner.screen.json": _doc(),
        },
    )

    assert [(r["code"], r["path"]) for r in refusals] == [("reserved_segment", f"{segment}/inner.screen.json")]


@pytest.mark.parametrize("name", ["-logo.png", "notes.log.png", "x.pid.png"])
async def test_reserved_endings_apply_to_whole_segments_only(settings, name: str) -> None:
    """``-logo.png`` starts with ``-``; a name that merely contains ``.log`` or ``.pid`` is taken."""
    files = {"rf/view.screen.json": _doc(_image("i", f"img/{name}")), f"rf/img/{name}": PNG}
    if name.startswith("-"):
        refusals = await _grammar_refusals(settings, files)
        assert [(r["code"], r["path"]) for r in refusals] == [("reserved_segment", f"img/{name}")]
    else:
        _put(settings, files)
        assert (await request_push("rf/view", "sr/rf", "note"))["files"] == [f"img/{name}", "view.screen.json"]


@pytest.mark.parametrize("directory", ["x.log", ".hidden", "CLAUDE.local.md", "a.pyc"])
async def test_an_image_under_a_reserved_directory_is_refused(settings, directory: str) -> None:
    refusals = await _grammar_refusals(
        settings,
        {
            "rf/view.screen.json": _doc(_image("i", f"{directory}/logo.png")),
            f"rf/{directory}/logo.png": PNG,
        },
    )

    assert [(r["code"], r["path"]) for r in refusals] == [("reserved_segment", f"{directory}/logo.png")]


async def test_an_svg_is_refused(settings) -> None:
    refusals = await _grammar_refusals(
        settings,
        {"rf/view.screen.json": _doc(_image("i", "img/logo.SVG")), "rf/img/logo.SVG": b"<svg/>"},
    )

    assert [(r["code"], r["path"]) for r in refusals] == [("suffix", "img/logo.SVG")]


async def test_a_file_over_the_per_file_cap_is_refused(settings, monkeypatch) -> None:
    assert MAX_ASSET_BYTES == 5 * 2**20
    top = _doc(_image("a", "ok.png"), _image("b", "big.png"))
    cap = len(_bytes(top))
    monkeypatch.setattr(push, "MAX_ASSET_BYTES", cap)
    files = {"rf/view.screen.json": top, "rf/ok.png": PNG, "rf/big.png": PNG + b"\x00" * cap}

    refusals = await _grammar_refusals(settings, files)

    assert [(r["code"], r["path"]) for r in refusals] == [("file_too_large", "big.png")]


async def test_the_read_stops_at_the_first_file_over_a_cap(settings, monkeypatch) -> None:
    """Nothing past the cap is read under the store lock: of a file over it, one byte more than the cap."""
    top = _doc(_image("a", "big.png"), _image("b", "never.png"))
    cap = len(_bytes(top))
    monkeypatch.setattr(push, "MAX_ASSET_BYTES", cap)
    _put(settings, {"rf/view.screen.json": top, "rf/big.png": PNG + b"\x00" * (10 * cap), "rf/never.png": PNG})
    trees: list[push._CappedTree] = []
    real = push.push_closure

    def closure(tree, *args, **kwargs):
        trees.append(tree)
        return real(tree, *args, **kwargs)

    monkeypatch.setattr(push, "push_closure", closure)

    result = await request_push("rf/view", "sr/rf", "note")

    assert [r["code"] for r in result["refusals"]] == ["file_too_large"]
    assert sorted(trees[0].recorded) == ["screens/rf/view.screen.json"]
    assert len(trees) == 1 and not _is_locked(settings.drafts_dir)


def test_grammar_applies_the_caps_to_the_bytes_it_is_given(monkeypatch) -> None:
    monkeypatch.setattr(push, "MAX_ASSET_BYTES", len(PNG))
    monkeypatch.setattr(push, "MAX_FILES", 2)
    monkeypatch.setattr(push, "MAX_TOTAL_BYTES", 2 * len(PNG))

    found = push._grammar({"a.png": PNG, "b.png": PNG + b"\x00", "c.png": PNG})

    assert [(r["code"], r["path"]) for r in found] == [
        ("file_too_large", "b.png"),
        ("too_many_files", ""),
        ("bundle_too_large", ""),
    ]


async def test_more_files_than_the_count_cap_are_refused(settings, monkeypatch) -> None:
    assert push.MAX_FILES == 256
    monkeypatch.setattr(push, "MAX_FILES", 2)
    files = {
        "rf/view.screen.json": _doc(_image("a", "a.png"), _image("b", "b.png")),
        "rf/a.png": PNG,
        "rf/b.png": PNG,
    }

    assert [r["code"] for r in await _grammar_refusals(settings, files)] == ["too_many_files"]


async def test_exactly_the_count_cap_is_taken(settings, monkeypatch) -> None:
    monkeypatch.setattr(push, "MAX_FILES", 2)
    _put(settings, {"rf/view.screen.json": _doc(_image("a", "a.png")), "rf/a.png": PNG})

    assert len((await request_push("rf/view", "sr/rf", "note"))["files"]) == 2


async def test_more_bytes_than_the_total_cap_are_refused(settings, monkeypatch) -> None:
    assert push.MAX_TOTAL_BYTES == 32 * 2**20
    files = {"rf/view.screen.json": _doc(_image("a", "a.png")), "rf/a.png": PNG}
    monkeypatch.setattr(push, "MAX_TOTAL_BYTES", len(_bytes(files["rf/view.screen.json"])) + len(PNG) - 1)

    assert [r["code"] for r in await _grammar_refusals(settings, files)] == ["bundle_too_large"]


@pytest.mark.parametrize("char", ["\x1b", "\x00", "\x7f", "\x85", "\u202e", "\u200b", "\ufeff"])
async def test_a_forbidden_character_in_any_screen_string_is_refused_with_its_pointer(settings, char: str) -> None:
    refusals = await _grammar_refusals(
        settings,
        {
            "rf/view.screen.json": _doc(_embed("e", "inner.screen.json")),
            "rf/inner.screen.json": _doc(_node("l", props={"text": f"a{char}b"})),
        },
    )

    assert [(r["code"], r["path"], r["pointer"]) for r in refusals] == [
        ("invalid_text", "inner.screen.json", "/root/children/0/props/text")
    ]


async def test_a_forbidden_character_in_a_key_or_the_metadata_is_refused(settings) -> None:
    doc = _doc(title="RF\u202e")
    doc["x-notes"] = {"k\x07": 1}

    refusals = await _grammar_refusals(settings, {"rf/view.screen.json": doc})

    assert [(r["code"], r["pointer"]) for r in refusals] == [("invalid_text", "/metadata/title")]


async def test_tab_and_newlines_in_screen_text_are_taken(settings) -> None:
    _put(settings, {"view.screen.json": _doc(_node("l", props={"text": "a\tb\nc\r\nd"}))})

    assert "refused" not in await request_push("view", "sr", "note")


async def test_directories_that_differ_only_in_case_are_refused(settings) -> None:
    """Holds on a case-insensitive host too: the paths are the refs as written."""
    refusals = await _grammar_refusals(
        settings,
        {
            "rf/view.screen.json": _doc(_embed("a", "Parts/a.screen.json"), _embed("b", "parts/b.screen.json")),
            "rf/Parts/a.screen.json": _doc(),
            "rf/parts/b.screen.json": _doc(),
        },
    )

    assert [(r["code"], r["paths"]) for r in refusals] == [("case_collision", ["Parts", "parts"])]


def test_grammar_takes_a_clean_set() -> None:
    files = {"view.screen.json": _bytes(_doc()), "img/Logo 1.v2.PNG": PNG, "a-b/c_d.screen.json": _bytes(_doc())}

    assert push._grammar(files) == []


@pytest.mark.parametrize(
    ("files", "codes"),
    [
        ({"a.screen.json": b"{}", "A.screen.json": b"{}"}, ["case_collision"]),
        ({"img/a.png": PNG, "IMG/b.png": PNG}, ["case_collision"]),
        ({"stra\u00dfe.png": PNG, "strasse.png": PNG}, ["bad_segment", "case_collision"]),
        ({"a.png": PNG, "a.png/b.png": PNG}, ["case_collision"]),
        ({"a.b.screen.json": b"{}"}, ["bad_segment"]),
        ({"lo+go.png": PNG}, ["bad_segment"]),
        ({"a.txt": b"x"}, ["suffix"]),
        ({"a": b"x"}, ["suffix"]),
        ({"a.svg": b"<svg/>"}, ["suffix"]),
        ({".git/a.png": PNG}, ["reserved_segment"]),
        ({"x.swp/a.png": PNG}, ["reserved_segment"]),
        ({"var.screen.json": b"{}"}, ["reserved_segment"]),
        ({"a.screen.json": b'{"t": "\\u0007"}'}, ["invalid_text"]),
        ({"a.screen.json": b'{"t": "\\ud800"}'}, ["invalid_text"]),
    ],
)
def test_grammar_names_what_is_broken(files: dict, codes: list[str]) -> None:
    found = push._grammar(files, IGNORED)

    assert sorted(r["code"] for r in found) == sorted(codes)
    assert set(codes) <= set(push.GRAMMAR_CODES)


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        (json.dumps({"t": "plain"}).encode("utf-16"), ""),
        (json.dumps({"t": "plain"}).encode("utf-32-le"), ""),
        (b"\xef\xbb\xbf" + json.dumps({"t": "plain"}).encode(), ""),
        (b'{"t": "\xff"}', ""),
        # The first value is dropped by every JSON reader, and is still in the file.
        ('{"t": "a\u202eb", "t": "plain"}'.encode(), ""),
        ('{"t": "a\u202eb"}'.encode(), "/t"),
        (b'{"t": "a\\u202eb"}', "/t"),
    ],
)
def test_screen_bytes_are_judged_as_well_as_the_document_they_parse_to(data: bytes, pointer: str) -> None:
    found = push._grammar({"a.screen.json": data})

    assert [(r["code"], r["pointer"]) for r in found] == [("invalid_text", pointer)]


def test_non_ascii_text_in_a_screen_is_taken() -> None:
    doc = _doc(_node("l", props={"text": "Strahlführung — 電子"}))

    assert push._grammar({"a.screen.json": json.dumps(doc, ensure_ascii=False).encode()}) == []


async def test_the_size_judged_is_the_size_written(settings, view, monkeypatch) -> None:
    first = await request_push("rf/view", "sr/rf", "note")
    size = (Path(settings.push_dir) / first["rid"] / "request.json").stat().st_size

    monkeypatch.setattr(push, "MAX_REQUEST_BYTES", size)
    assert "refused" not in await request_push("rf/view", "sr/rf", "note")
    monkeypatch.setattr(push, "MAX_REQUEST_BYTES", size - 1)
    assert (await request_push("rf/view", "sr/rf", "note"))["refusals"][0]["code"] == "request_too_large"


async def test_a_request_too_long_for_the_reviewers_listing_is_refused(settings, view, monkeypatch) -> None:
    assert push.MAX_REQUEST_BYTES == 64 * 2**10
    monkeypatch.setattr(push, "MAX_REQUEST_BYTES", 200)

    result = await request_push("rf/view", "sr/rf", "note")

    assert (result["reason"], [r["code"] for r in result["refusals"]]) == ("grammar_refused", ["request_too_large"])
    assert not Path(settings.push_dir).exists()


# --- check errors ---------------------------------------------------------------------


async def test_an_error_finding_in_any_pushed_screen_refuses(settings) -> None:
    broken = _doc(_node("dup"), _node("dup"))
    _put(
        settings,
        {"rf/view.screen.json": _doc(_embed("e", "inner.screen.json")), "rf/inner.screen.json": broken},
    )

    result = await request_push("rf/view", "sr/rf", "note")

    assert (result["refused"], result["reason"]) == (True, "check_errors")
    assert result["errors"] and {e["screen"] for e in result["errors"]} == {"rf/inner"}
    assert all(e["severity"] == "error" for e in result["errors"])
    assert not Path(settings.push_dir).exists()


# --- one state of the store -----------------------------------------------------------


async def test_a_store_write_after_the_read_does_not_change_the_bundle(settings, view, monkeypatch) -> None:
    """The check runs after the lock is released; what it and the bundle see is what was read."""
    store = _common.get_store(settings)
    real = push.run_check
    old_rev = store.rev("rf/view")
    seen: list[str] = []

    async def write_then_check(*args, **kwargs):
        if not seen:
            await store.write(
                "rf/parts/inner", _doc(_node("l", props={"text": "changed"})), store.rev("rf/parts/inner")
            )
            await store.write("rf/view", _doc(_node("l", props={"text": "no embed any more"})), old_rev)
            (Path(settings.drafts_dir) / "screens" / "rf" / "img" / "logo.png").write_bytes(PNG + b"new")
        seen.append(kwargs["draft_id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(push, "run_check", write_then_check)

    result = await request_push("rf/view", "sr/rf", "note")

    assert seen == ["rf/parts/inner", "rf/view"]
    assert store.rev("rf/view") != old_rev
    assert result["rev"] == old_rev
    bundle = Path(settings.push_dir) / result["rid"]
    for member in ("img/logo.png", "parts/inner.screen.json", "view.screen.json"):
        assert (bundle / "files" / member).read_bytes() == view[f"rf/{member}"]
    request = json.loads((bundle / "request.json").read_bytes())
    assert request["rev"] == old_rev
    assert [f["sha256"] for f in request["files"]] == [
        hashlib.sha256(view[f"rf/{m}"]).hexdigest()
        for m in ("img/logo.png", "parts/inner.screen.json", "view.screen.json")
    ]


async def test_the_check_reads_the_recorded_bytes_not_the_store(settings, view, monkeypatch) -> None:
    """A screen broken in the store after the read does not refuse the push, and is not in it."""
    store = _common.get_store(settings)
    real = push.push_closure
    calls: list[str] = []

    def closure(tree, *args, **kwargs):
        calls.append("store" if isinstance(tree, StoreTree) else "buffer")
        members = real(tree, *args, **kwargs)
        if len(calls) == 2:  # the lock is free: the read is over
            target = Path(settings.drafts_dir) / "screens" / "rf" / "parts" / "inner.screen.json"
            target.write_bytes(_bytes(_doc(_node("dup"), _node("dup"))))
        return members

    monkeypatch.setattr(push, "push_closure", closure)

    result = await request_push("rf/view", "sr/rf", "note")

    assert calls == ["store", "buffer"]
    assert "refused" not in result
    assert store.rev("rf/parts/inner") != hashlib.sha256(view["rf/parts/inner.screen.json"]).hexdigest()


# --- the locks ------------------------------------------------------------------------


class _Parked:
    """Parks the first call of ``module.<name>`` (made in a worker thread) until released."""

    def __init__(self, monkeypatch, module, name: str):
        self.entered = threading.Event()
        self.release = threading.Event()
        real = getattr(module, name)

        def parked(*args, **kwargs):
            if not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(30), "the test never released the parked call"
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, parked)

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 30)


async def test_the_read_holds_the_store_lock_and_a_writer_waits_for_it(settings, view, monkeypatch) -> None:
    store = _common.get_store(settings)
    parked = _Parked(monkeypatch, push, "push_closure")

    pushing = asyncio.create_task(request_push("rf/view", "sr/rf", "note"))
    await parked.wait_entered()
    assert _is_locked(settings.drafts_dir)
    writing = asyncio.create_task(store.write("rf/unrelated", _doc(title="later"), store.rev("rf/unrelated")))
    await asyncio.sleep(0.2)
    assert not writing.done() and not pushing.done()

    parked.release.set()
    result = await asyncio.wait_for(pushing, 30)
    await asyncio.wait_for(writing, 30)

    assert "refused" not in result
    assert not _is_locked(settings.drafts_dir)


async def test_two_reads_share_the_store_lock(settings, view, monkeypatch) -> None:
    parked = _Parked(monkeypatch, push, "push_closure")

    first = asyncio.create_task(request_push("rf/view", "sr/rf", "first"))
    await parked.wait_entered()
    second = await asyncio.wait_for(request_push("rf/view", "sr/rf", "second"), 30)
    assert not first.done()
    parked.release.set()

    assert "refused" not in second and "refused" not in await asyncio.wait_for(first, 30)


async def test_a_store_write_is_not_blocked_while_the_check_waits_on_a_slow_pv_directory(
    settings, httpx_mock, monkeypatch
) -> None:
    files = _put(settings, {"rf/view.screen.json": _doc(_node("p", "pv-label", {"pv": "SR:RF:POWER"}))})
    store = _common.get_store(settings)
    old_rev = store.rev("rf/view")
    asked = asyncio.Event()
    answer = asyncio.Event()

    async def slow_raccoon(request: httpx.Request) -> httpx.Response:
        asked.set()
        await answer.wait()
        return httpx.Response(200, json={"known": ["SR:RF:POWER"], "unknown": []})

    httpx_mock.add_callback(slow_raccoon, url=RACCOON, method="POST")

    pushing = asyncio.create_task(request_push("rf/view", "sr/rf", "note"))
    await asyncio.wait_for(asked.wait(), 30)
    # The check is waiting: no lock is held, and a save goes through at once.
    assert not _is_locked(settings.drafts_dir)
    assert not Path(settings.push_dir).exists()
    new_rev = await asyncio.wait_for(store.write("rf/view", _doc(title="saved meanwhile"), old_rev), 5)
    assert new_rev != old_rev and not pushing.done()

    answer.set()
    result = await asyncio.wait_for(pushing, 30)

    assert result["rev"] == old_rev
    bundle = Path(settings.push_dir) / result["rid"]
    assert (bundle / "files" / "view.screen.json").read_bytes() == files["rf/view.screen.json"]


async def test_the_bundle_is_written_under_the_push_dir_lock_only(settings, view, monkeypatch) -> None:
    store = _common.get_store(settings)
    parked = _Parked(monkeypatch, push, "write_atomic")

    pushing = asyncio.create_task(request_push("rf/view", "sr/rf", "note"))
    await parked.wait_entered()
    assert _is_locked(settings.push_dir)
    assert not _is_locked(settings.drafts_dir)
    await asyncio.wait_for(store.write("rf/unrelated", _doc(title="later"), store.rev("rf/unrelated")), 5)
    (bundle,) = [p for p in Path(settings.push_dir).iterdir()]
    assert not (bundle / "request.json").exists()  # written last

    parked.release.set()
    result = await asyncio.wait_for(pushing, 30)

    assert bundle.name == result["rid"] and (bundle / "request.json").is_file()
    assert not _is_locked(settings.push_dir)


async def test_request_json_is_the_last_file_written(settings, view, monkeypatch) -> None:
    written: list[str] = []
    real = push.write_atomic

    def recording(path, data):
        written.append(path.name)
        return real(path, data)

    monkeypatch.setattr(push, "write_atomic", recording)

    result = await request_push("rf/view", "sr/rf", "note")

    assert written == ["logo.png", "inner.screen.json", "view.screen.json", "request.json"]
    assert not [p for p in (Path(settings.push_dir) / result["rid"]).rglob("*.tmp")]


# --- failures at the write ------------------------------------------------------------


async def test_a_bundle_that_cannot_be_completed_is_removed(settings, view, monkeypatch) -> None:
    real = push.write_atomic

    def failing(path, data):
        if path.name == "request.json":
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(push, "write_atomic", failing)

    with pytest.raises(OSError, match="No space left"):
        await request_push("rf/view", "sr/rf", "note")

    assert _tree(Path(settings.push_dir)) == []
    assert not _is_locked(settings.push_dir)


async def test_an_unwritable_push_dir_answers_push_unavailable_without_its_path(settings, view, monkeypatch) -> None:
    def unwritable(path):
        raise ReadOnlyStore(f"{path}: Read-only file system")

    monkeypatch.setattr(push, "make_dirs", unwritable)

    result = await request_push("rf/view", "sr/rf", "note")

    assert (result["refused"], result["reason"]) == (True, "push_unavailable")
    assert str(settings.push_dir) not in json.dumps(result)


# --- the queue --------------------------------------------------------------------------


def _old_bundle(push_dir: Path, name: str, age: int) -> None:
    """A complete bundle whose ``request.json`` was written ``age`` hours ago."""
    bundle = push_dir / name
    (bundle / "files").mkdir(parents=True)
    (bundle / "files" / "view.screen.json").write_bytes(b"{}")
    request = bundle / "request.json"
    request.write_text(json.dumps({"format": "canopy-push/1", "rid": name}))
    stamp = request.stat().st_mtime - 3600 * age
    os.utime(request, (stamp, stamp))


async def test_a_full_queue_refuses_the_push_and_deletes_no_bundle(settings, view) -> None:
    """Eight requests wait for review: the ninth is refused, and every waiting request stays."""
    push_dir = Path(settings.push_dir)
    for age in range(1, 9):
        _old_bundle(push_dir, f"{age:016x}", age)
    before = _tree(push_dir)

    result = await request_push("rf/view", "sr/rf", "note")

    assert (result["refused"], result["reason"]) == (True, "push_queue_full")
    assert _tree(push_dir) == before


async def test_the_eighth_request_fits_and_the_ninth_is_refused(settings, view) -> None:
    push_dir = Path(settings.push_dir)
    for age in range(1, 8):
        _old_bundle(push_dir, f"{age:016x}", age)

    eighth = await request_push("rf/view", "sr/rf", "eighth")
    ninth = await request_push("rf/view", "sr/rf", "ninth")

    assert "refused" not in eighth and ninth["reason"] == "push_queue_full"
    assert len(list(push_dir.iterdir())) == push.MAX_PENDING_BUNDLES
    # once the reviewer removes one, the next push is filed
    shutil.rmtree(push_dir / f"{1:016x}")
    assert "rid" in await request_push("rf/view", "sr/rf", "again")


async def test_a_directory_without_request_json_is_deleted_and_never_counted(settings, view) -> None:
    push_dir = Path(settings.push_dir)
    for age in range(1, 8):
        _old_bundle(push_dir, f"{age:016x}", age)
    (push_dir / "interrupted" / "files").mkdir(parents=True)
    (push_dir / "interrupted" / "files" / "view.screen.json").write_bytes(b"{}")
    (push_dir / "empty").mkdir()
    (push_dir / "stray.txt").write_text("not a bundle")

    result = await request_push("rf/view", "sr/rf", "note")

    assert "rid" in result  # the two leftovers do not count against the queue
    names = {p.name for p in push_dir.iterdir()}
    assert "interrupted" not in names and "empty" not in names
    assert "stray.txt" in names
    assert len(names - {"stray.txt"}) == 8


async def test_a_refused_push_changes_nothing(settings, monkeypatch) -> None:
    push_dir = Path(settings.push_dir)
    for age in range(1, 10):
        _old_bundle(push_dir, f"{age:016x}", age)
    (push_dir / "interrupted").mkdir()
    _put(settings, {"rf/view.screen.json": _doc(_embed("gone", "absent.screen.json"))})
    before = _tree(push_dir)

    result = await request_push("rf/view", "sr/rf", "note")

    assert result["reason"] == "closure_refused"
    assert _tree(push_dir) == before


async def test_a_bundle_name_already_taken_is_never_reused(settings, view, monkeypatch) -> None:
    push_dir = Path(settings.push_dir)
    _old_bundle(push_dir, "aa" * 8, 1)
    rids = iter(["aa" * 8, "bb" * 8])
    monkeypatch.setattr(push.secrets, "token_hex", lambda n: next(rids))

    result = await request_push("rf/view", "sr/rf", "note")

    assert result["rid"] == "bb" * 8
    assert json.loads((push_dir / ("aa" * 8) / "request.json").read_text())["rid"] == "aa" * 8
