"""convert_legacy (FR5 / SC7): legacy parent + sibling conversion into drafts, with a work queue."""

import io
import os
import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock
from fastmcp.exceptions import ToolError

from canopy_mcp import refs
from canopy_mcp.store import MAX_ASSET_BYTES
from canopy_mcp.tools import drafts, convert
from canopy_mcp.config import Settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.tools.check import run_check
from canopy_mcp.tools.drafts import get_screen, patch_node, list_drafts
from canopy_mcp.tools.convert import QUEUE_CODES, convert_legacy
from canopy.core.color_scheme import THEME_FILE_ENV, ColorScheme
from tests.canopy_mcp.fixtures.convert import mocks

BASE = "http://canopy.test"
PARENT_ID = "a/parent"
SIBLINGS_WRITTEN = {"a/embedded/_warn", "a/embedded/_drop", "a/def"}
ALL_WRITTEN = {PARENT_ID, *SIBLINGS_WRITTEN}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(base_url=BASE, drafts_dir=tmp_path / "drafts")
    monkeypatch.setattr(drafts, "get_settings", lambda: s)
    monkeypatch.setattr(convert, "get_settings", lambda: s)
    drafts.reset_stores()
    ir_map = {
        t: {"consumes": [], "verbatim": False}
        for t in ("absolute-canvas", "group", "label", "regular-button", "action-button", "embedded-display")
    }
    set_catalog(build_catalog([], ir_map))
    yield s
    set_catalog(None)
    drafts.reset_stores()


@pytest.fixture
def converter(httpx_mock: HTTPXMock):
    mocks.install_converter(httpx_mock, BASE)
    return httpx_mock


@pytest.fixture
def sources(tmp_path):
    return mocks.write_sources(tmp_path / "src")


def _siblings(sources):
    return {"embedded/_warn.bob": str(sources["warn"]), "embedded/_drop.bob": str(sources["drop"])}


def _file(settings, draft_id):
    return settings.drafts_dir / "screens" / f"{draft_id}.screen.json"


def _written(settings):
    root = settings.drafts_dir / "screens"
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix()[: -len(".screen.json")] for p in root.rglob("*.screen.json")}


async def _convert(sources, **kw):
    kw.setdefault("siblings", _siblings(sources))
    return await convert_legacy(id=PARENT_ID, path=str(sources["parent"]), kind="bob", **kw)


async def _queue_of_check(draft_id, settings):
    """``check(draft_id)`` findings limited to the queue codes, tagged the way the queue tags them."""
    result = await run_check(draft_id=draft_id, store=drafts.get_store(settings), settings=settings)
    return [{"draftId": draft_id, **f.to_dict()} for f in result.findings if f.code in QUEUE_CODES]


# --- refusals before any conversion ------------------------------------------------------


async def test_relative_path_refuses_naming_path_and_posts_nothing(settings, converter, sources):
    out = await convert_legacy(id=PARENT_ID, path="src/panel/parent.bob", kind="bob", siblings=_siblings(sources))
    assert out["refused"] is True
    assert out["keys"] == ["path"]
    assert "path" in out["message"]
    assert mocks.conversion_requests(converter) == []
    assert _written(settings) == set()


async def test_relative_source_path_refuses_naming_that_key_and_posts_nothing(settings, converter, sources):
    siblings = {**_siblings(sources), "embedded/_drop.bob": "common/embedded/_drop.bob"}
    out = await _convert(sources, siblings=siblings)
    assert out["refused"] is True
    assert out["keys"] == ["embedded/_drop.bob"]
    assert "embedded/_drop.bob" in out["message"]
    assert mocks.conversion_requests(converter) == []
    assert _written(settings) == set()


async def test_tilde_paths_expand_to_absolute(settings, converter, sources, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = await convert_legacy(
        id=PARENT_ID,
        path="~/src/panel/parent.bob",
        kind="bob",
        siblings={"embedded/_warn.bob": "~/src/common/embedded/_warn.bob"},
    )
    assert "refused" not in out
    assert {"a/parent", "a/embedded/_warn", "a/def"} <= _written(settings)


@pytest.mark.parametrize("bad", ["", "a//b", "a/../b", "/a", "a b", "a.b"])
async def test_invalid_id_is_rejected(settings, converter, sources, bad):
    with pytest.raises(ToolError):
        await convert_legacy(id=bad, path=str(sources["parent"]), kind="bob")
    assert mocks.conversion_requests(converter) == []


async def test_path_and_content_are_mutually_exclusive(settings, converter, sources):
    with pytest.raises(ToolError):
        await convert_legacy(id="a/b", path=str(sources["parent"]), content=mocks.PARENT, kind="bob")
    with pytest.raises(ToolError):
        await convert_legacy(id="a/b", kind="bob")
    assert mocks.conversion_requests(converter) == []


# --- what is written -------------------------------------------------------------------------


async def test_writes_parent_and_resolvable_siblings_only(settings, converter, sources):
    out = await _convert(sources)
    assert out["draftId"] == PARENT_ID
    assert out["rev"] and out["url"].endswith(f"/embed/drafts/{PARENT_ID}")
    # absolute, above-root, invalid-id and missing-default-source refs are not written
    assert _written(settings) == ALL_WRITTEN
    assert {d["draftId"] for d in out["drafts"]} == ALL_WRITTEN
    # one synchronous conversion per written file
    assert sorted(mocks.uploaded_filenames(converter)) == ["_drop.bob", "_warn.bob", "def.bob", "parent.bob"]


async def test_each_document_is_stamped_with_id_and_source_stem_title(settings, converter, sources):
    await _convert(sources)
    expected_titles = {PARENT_ID: "parent", "a/embedded/_warn": "_warn", "a/embedded/_drop": "_drop", "a/def": "def"}
    for draft_id, title in expected_titles.items():
        doc = json.loads(_file(settings, draft_id).read_text())
        assert doc["id"] == draft_id
        assert doc["metadata"]["title"] == title


async def test_content_title_is_last_id_segment_and_is_listed(settings, converter):
    out = await convert_legacy(id="a/b", content=mocks.SIBLING_DEFAULT, kind="bob")
    assert out["draftId"] == "a/b"
    doc = json.loads(_file(settings, "a/b").read_text())
    assert doc["metadata"]["title"] == "b"
    assert doc["id"] == "a/b"
    listed = {d["draftId"]: d["title"] for d in (await list_drafts())["drafts"]}
    assert listed == {"a/b": "b"}


async def test_content_refs_without_siblings_are_missing(settings, converter):
    out = await convert_legacy(id="a/p", content=mocks.display(mocks.embedded("E", "x.bob")), kind="bob")
    assert _written(settings) == {"a/p"}
    reasons = [i["reason"] for i in out["queue"] if i["code"] == "unresolved_ref"]
    assert reasons == ["missing"]


async def test_sibling_key_is_matched_after_normalization(settings, converter, sources):
    siblings = {"./embedded/../embedded/_warn.bob": str(sources["warn"])}
    await _convert(sources, siblings=siblings)
    assert "a/embedded/_warn" in _written(settings)
    assert "a/embedded/_drop" not in _written(settings)  # no entry and no default source beside the parent


async def test_siblings_own_refs_are_never_followed(settings, converter, tmp_path):
    src = tmp_path / "nest"
    src.mkdir()
    (src / "top.bob").write_text(mocks.display(mocks.embedded("N", "nested.bob")))
    (src / "nested.bob").write_text(mocks.SIBLING_WITH_REF)
    (src / "deeper.bob").write_text(mocks.SIBLING_DEFAULT)  # exists, but only the parent's refs are followed
    out = await convert_legacy(id="n/top", path=str(src / "top.bob"), kind="bob")
    assert _written(settings) == {"n/top", "n/nested"}
    assert sorted(mocks.uploaded_filenames(converter)) == ["nested.bob", "top.bob"]
    # check reports the sibling's ref under the sibling, and under the parent with docId
    unresolved = [i for i in out["queue"] if i["code"] == "unresolved_ref"]
    assert {(i["draftId"], i.get("docId"), i["reason"]) for i in unresolved} == {
        ("n/nested", None, "missing"),
        ("n/top", "n/nested", "missing"),
    }


# --- the queue ---------------------------------------------------------------------------------


async def test_one_unresolved_ref_per_reason_and_the_call_succeeds(settings, converter, sources):
    out = await _convert(sources)
    assert "refused" not in out
    unresolved = [i for i in out["queue"] if i["code"] == "unresolved_ref"]
    assert sorted(i["reason"] for i in unresolved) == ["above_root", "absolute", "invalid_id", "missing"]
    assert {i["draftId"] for i in unresolved} == {PARENT_ID}
    parent_check = [i for i in await _queue_of_check(PARENT_ID, settings) if i["code"] == "unresolved_ref"]
    assert unresolved == parent_check


async def test_queue_equals_check_per_draft_plus_dropped_props(settings, converter, sources):
    out = await _convert(sources)
    queue = out["queue"]
    assert all("draftId" in item for item in queue)
    assert {i["code"] for i in queue} <= set(QUEUE_CODES) | {"dropped_prop"}
    for draft_id in ALL_WRITTEN:
        mine = [i for i in queue if i["draftId"] == draft_id and i["code"] != "dropped_prop"]
        assert mine == await _queue_of_check(draft_id, settings)


async def test_warning_and_dropped_prop_items(settings, converter, sources):
    out = await _convert(sources)
    queue = out["queue"]
    warnings = [i for i in queue if i["code"] == "warning"]
    assert [i["draftId"] for i in warnings] == ["a/embedded/_warn"]
    assert "execute" in warnings[0]["message"]
    dropped = [i for i in queue if i["code"] == "dropped_prop"]
    assert len(dropped) == 1
    item = dropped[0]
    assert set(item) == {"draftId", "nodeId", "code", "props"}
    assert item["draftId"] == "a/embedded/_drop"
    assert item["props"] == ["tooltip"]
    drop_doc = json.loads(_file(settings, "a/embedded/_drop").read_text())
    ids = [c["id"] for c in drop_doc["root"].get("children", [])]
    assert item["nodeId"] in ids


async def test_queue_omits_inert_and_lint_findings(settings, converter):
    # a write_pv button (inert_in_osprey) on a PV Raccoon calls unknown (lint), and an unmapped 'tank'
    converter.add_response(
        method="POST",
        url=BASE + "/raccoon/v1/pvs/validate",
        json={"known": [], "unknown": ["A:B"]},
        is_optional=True,
        is_reusable=True,
    )
    body = mocks.display(
        '  <widget type="action_button" version="3.0.0"><name>W</name><x>0</x><y>0</y><pv_name>A:B</pv_name>'
        '<actions><action type="write_pv"><pv_name>A:B</pv_name><value>1</value></action></actions></widget>\n'
        '  <widget type="tank" version="2.0.0"><name>T</name><x>0</x><y>40</y></widget>\n'
    )
    out = await convert_legacy(id="z/one", content=body, kind="bob")
    checked = await run_check(draft_id="z/one", store=drafts.get_store(settings), settings=settings)
    check_codes = {f.code for f in checked.findings}
    assert "inert_in_osprey" in check_codes and checked.lint  # present in check...
    queue_codes = {i["code"] for i in out["queue"]}
    assert "inert_in_osprey" not in queue_codes  # ...but not in the queue
    assert not {f.code for f in checked.lint} & queue_codes
    unknown = [i for i in out["queue"] if i["code"] == "unknown_widget"]
    assert [i["originalClass"] for i in unknown] == ["tank"]
    assert out["queue"][: len(await _queue_of_check("z/one", settings))] == await _queue_of_check("z/one", settings)


def test_description_names_both_dropped_prop_vocabularies():
    doc = convert_legacy.__doc__ or ""
    assert "dropped_prop" in doc
    assert "source vocabulary" in doc
    assert "Qt vocabulary" in doc


# --- revs: reuse, conflicts, all-or-nothing ------------------------------------------------------


async def test_rerun_reuses_identical_drafts_without_rewriting(settings, converter, sources):
    first = await _convert(sources)
    mtimes = {d: _file(settings, d).stat().st_mtime_ns for d in ALL_WRITTEN}
    second = await _convert(sources)
    assert "refused" not in second
    assert {d["draftId"]: d["rev"] for d in second["drafts"]} == {d["draftId"]: d["rev"] for d in first["drafts"]}
    assert second["rev"] == first["rev"]
    assert {d: _file(settings, d).stat().st_mtime_ns for d in ALL_WRITTEN} == mtimes
    assert second["queue"] == first["queue"]


async def test_overwrite_with_empty_base_revs_refuses_listing_the_edited_draft(settings, converter, sources):
    await _convert(sources)
    current = await get_screen(PARENT_ID)
    title_node = current["screen"]["root"]["children"][0]["id"]
    patched = await patch_node(PARENT_ID, title_node, current["rev"], {"geometry": {"x": 5}})
    assert "refused" not in patched
    before = {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN}

    out = await _convert(sources, overwrite=True, base_revs={})
    assert out["refused"] is True
    assert out["conflicts"] == [PARENT_ID]
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before

    out = await _convert(sources)  # overwrite=false refuses too, with the right rev in hand or not
    assert out["refused"] is True and out["conflicts"] == [PARENT_ID]

    out = await _convert(sources, base_revs={PARENT_ID: current["rev"]}, overwrite=True)  # stale rev
    assert out["refused"] is True and out["conflicts"] == [PARENT_ID]
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


async def test_overwrite_with_current_base_rev_rewrites(settings, converter, sources):
    first = await _convert(sources)
    current = await get_screen(PARENT_ID)
    node = current["screen"]["root"]["children"][0]["id"]
    patched = await patch_node(PARENT_ID, node, current["rev"], {"geometry": {"x": 5}})
    out = await _convert(sources, overwrite=True, base_revs={PARENT_ID: patched["rev"]})
    assert "refused" not in out
    assert out["rev"] == first["rev"]  # back to the converted bytes


async def test_refusal_is_all_or_nothing(settings, converter, sources):
    await _convert(sources)
    _file(settings, "a/def").unlink()  # would be re-created...
    current = await get_screen(PARENT_ID)
    node = current["screen"]["root"]["children"][0]["id"]
    await patch_node(PARENT_ID, node, current["rev"], {"geometry": {"x": 5}})
    out = await _convert(sources)  # ...but the parent conflicts, so nothing is written
    assert out["refused"] is True and out["conflicts"] == [PARENT_ID]
    assert not _file(settings, "a/def").exists()


# --- scheme guard on overwrite (CANOPY_THEME_FILE) ------------------------------------------------

OWN_META = {"id": "site", "sha256": "a" * 64, "flatHash": "b" * 64, "served": "site", "errors": []}
GOOD_SCHEME = {"id": "site", "sha256": "a" * 64, "served": "site", "errors": []}
MISSING = object()  # a scheme_converter value: drop the ``scheme`` key from the response


@pytest.fixture
def themed(monkeypatch, tmp_path):
    """``CANOPY_THEME_FILE`` set, with canopy_mcp's own scheme meta pinned to :data:`OWN_META`."""
    monkeypatch.setenv(THEME_FILE_ENV, str(tmp_path / "site-scheme.json"))
    stub = ColorScheme(result=None, flat_json=None, meta=OWN_META, imports={}, degraded=[])
    monkeypatch.setattr(convert, "get_color_scheme", lambda *a, **k: stub)


@pytest.fixture
def scheme_converter(httpx_mock: HTTPXMock):
    """A converter whose response ``scheme`` is ``schemes[filename]`` (else ``schemes[None]``; key dropped if MISSING)."""
    schemes: dict = {None: GOOD_SCHEME}

    def respond(request):
        data, filename, kind = mocks._multipart(request)
        body = mocks.conversion_response(data, kind or Path(filename or "").suffix.lstrip("."))
        scheme = schemes.get(filename, schemes[None])
        body.pop("scheme", None)
        if scheme is not MISSING:
            body["scheme"] = scheme
        return httpx.Response(200, json=body)

    httpx_mock.add_callback(
        respond, method="POST", url=BASE + convert.CONVERSIONS_PATH, is_optional=True, is_reusable=True
    )
    return schemes


async def _edited(settings, sources):
    """Convert once, then hand-edit the parent; returns ``(before bytes, base_revs for an overwrite)``."""
    await _convert(sources)
    current = await get_screen(PARENT_ID)
    node = current["screen"]["root"]["children"][0]["id"]
    patched = await patch_node(PARENT_ID, node, current["rev"], {"geometry": {"x": 5}})
    return {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN}, {PARENT_ID: patched["rev"]}


async def test_overwrite_refused_when_converter_served_canopy_default(settings, scheme_converter, sources, themed):
    served = {**GOOD_SCHEME, "served": "canopy-default"}
    scheme_converter[None] = served
    before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert out["refused"] is True and out["reason"] == "scheme_mismatch" and "canopy-default" in out["message"]
    assert out["scheme"] == served
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


async def test_overwrite_refused_when_converter_scheme_has_errors(settings, scheme_converter, sources, themed):
    bad = {**GOOD_SCHEME, "errors": [{"path": "/roles/0", "message": "bad colour"}]}
    scheme_converter[None] = bad
    before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert out["refused"] is True and out["reason"] == "scheme_mismatch" and "error" in out["message"]
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


@pytest.mark.parametrize("field", ["id", "sha256"])
async def test_overwrite_refused_when_id_or_sha_differs_from_own(settings, scheme_converter, sources, themed, field):
    scheme_converter[None] = {**GOOD_SCHEME, field: "other"}
    before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert out["refused"] is True and out["reason"] == "scheme_mismatch" and "differs from" in out["message"]
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


async def test_overwrite_refused_when_a_sibling_disagrees_with_the_parent(settings, scheme_converter, sources, themed):
    scheme_converter["_warn.bob"] = {**GOOD_SCHEME, "sha256": "c" * 64}
    before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert out["refused"] is True and out["reason"] == "scheme_mismatch"
    assert "'a/embedded/_warn'" in out["message"] and "different scheme" in out["message"]
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


async def test_overwrite_refused_when_scheme_key_is_missing(settings, scheme_converter, sources, themed):
    scheme_converter[None] = MISSING
    before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert out["refused"] is True and out["reason"] == "scheme_mismatch" and "no scheme" in out["message"]
    assert out["scheme"] is None
    assert {d: _file(settings, d).read_bytes() for d in ALL_WRITTEN} == before


async def test_overwrite_passes_when_scheme_matches_and_returns_it(settings, scheme_converter, sources, themed):
    first = await _convert(sources)
    assert first["scheme"] == GOOD_SCHEME
    _before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert "refused" not in out
    assert out["rev"] == first["rev"]
    assert out["scheme"] == GOOD_SCHEME


async def test_without_overwrite_the_scheme_is_not_checked(settings, scheme_converter, sources, themed):
    scheme_converter[None] = {**GOOD_SCHEME, "served": "canopy-default"}
    out = await _convert(sources)
    assert "refused" not in out and _written(settings) == ALL_WRITTEN
    assert out["scheme"]["served"] == "canopy-default"


async def test_no_theme_file_leaves_overwrite_unchanged(settings, scheme_converter, sources, monkeypatch):
    monkeypatch.delenv(THEME_FILE_ENV, raising=False)
    monkeypatch.setattr(convert, "get_color_scheme", lambda *a, **k: pytest.fail("scheme consulted"))
    scheme_converter[None] = MISSING
    first = await _convert(sources)
    _before, base_revs = await _edited(settings, sources)
    out = await _convert(sources, overwrite=True, base_revs=base_revs)
    assert "refused" not in out and out["rev"] == first["rev"]
    assert out["scheme"] is None


# --- image copy (FR6) ------------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 40
SUCCESS = {"written", "reused", "replaced", "external"}


def picture(name: str, file: str, y: int = 0) -> str:
    return (
        f'  <widget type="picture" version="2.0.0"><name>{name}</name><x>0</x><y>{y}</y><file>{file}</file></widget>\n'
    )


def _plant(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _panel(root: Path, *refs: str, name: str = "panel.bob") -> Path:
    """A ``.bob`` under ``root`` holding one picture per ref, in order."""
    body = "".join(picture(f"P{i}", ref, 30 * i) for i, ref in enumerate(refs))
    return _plant(root / name, mocks.display(body).encode())


async def _port(panel: Path, draft_id: str = "a/panel", **kw):
    return await convert_legacy(id=draft_id, path=str(panel), kind="bob", **kw)


def _one(out):
    [asset] = out["assets"]
    return asset


def _asset_file(settings, relpath):
    return settings.drafts_dir / "screens" / relpath


def _assert_entry_shape(asset):
    assert set(asset) - {"detail"} == {"draftId", "ref", "path", "bytes", "status"}
    if asset["status"] in SUCCESS:
        assert "detail" not in asset
    else:
        assert isinstance(asset["detail"], str) and asset["detail"]


async def test_image_beside_the_source_is_copied_and_served(settings, converter, tmp_path):
    src = tmp_path / "src"
    _plant(src / "images" / "tv.png", PNG)
    out = await _port(_panel(src, "images/tv.png"))
    asset = _one(out)
    assert asset == {
        "draftId": "a/panel",
        "ref": "images/tv.png",
        "path": "screens/a/images/tv.png",
        "bytes": len(PNG),
        "status": "written",
    }
    served = drafts.get_store(settings).resolve_static(asset["path"])
    assert served is not None and served.read_bytes() == PNG
    assert served == _asset_file(settings, "a/images/tv.png").resolve()
    # a copied image leaves no unresolved_ref behind, in the queue or in check
    assert [i for i in out["queue"] if i["code"] == "unresolved_ref"] == []
    assert "assets" not in {i["code"] for i in out["queue"]}


async def test_missing_image_is_reported_and_the_queue_still_equals_check(settings, converter, tmp_path):
    src = tmp_path / "src"
    _plant(src / "images" / "ok.png", PNG)
    out = await _port(_panel(src, "images/ok.png", "images/gone.png"))
    by_ref = {a["ref"]: a for a in out["assets"]}
    assert by_ref["images/ok.png"]["status"] == "written"
    gone = by_ref["images/gone.png"]
    assert gone["status"] == "missing"
    assert gone["path"] == "screens/a/images/gone.png" and gone["bytes"] is None
    for asset in out["assets"]:
        _assert_entry_shape(asset)
    mine = [i for i in out["queue"] if i["code"] != "dropped_prop"]
    assert mine == await _queue_of_check("a/panel", settings)
    unresolved = [i for i in mine if i["code"] == "unresolved_ref"]
    assert [(i["reason"], i.get("kind")) for i in unresolved] == [("missing", "image")]


async def test_sibling_image_is_copied_from_the_siblings_own_tree(settings, converter, tmp_path):
    panel_dir = tmp_path / "panel"
    other = tmp_path / "elsewhere" / "embedded"
    _plant(other / "img" / "s.png", PNG)
    sib = _panel(other, "img/s.png", name="_sib.bob")
    parent = _plant(panel_dir / "top.bob", mocks.display(mocks.embedded("E", "embedded/_sib.bob")).encode())
    out = await _port(parent, draft_id="a/top", siblings={"embedded/_sib.bob": str(sib)})
    assert _one(out) == {
        "draftId": "a/embedded/_sib",
        "ref": "img/s.png",
        "path": "screens/a/embedded/img/s.png",
        "bytes": len(PNG),
        "status": "written",
    }
    assert _asset_file(settings, "a/embedded/img/s.png").read_bytes() == PNG


async def test_content_mode_has_no_source(settings, converter):
    out = await convert_legacy(id="a/c", content=mocks.display(picture("P", "images/tv.png")), kind="bob")
    asset = _one(out)
    assert asset["status"] == "no_source"
    assert asset["path"] == "screens/a/images/tv.png" and asset["bytes"] is None
    _assert_entry_shape(asset)
    assert not _asset_file(settings, "a/images/tv.png").exists()


async def test_refs_without_a_dest_short_circuit(settings, converter, tmp_path):
    src = tmp_path / "src"
    refs_ = ["platform:/plugin/x/icons/a.png", "HTTPS://x/A.png", "../../../up.png", "${IMG}.png"]
    out = await _port(_panel(src, *refs_))
    got = {a["ref"]: a for a in out["assets"]}
    assert [a["ref"] for a in out["assets"]] == refs_
    assert {r: a["status"] for r, a in got.items()} == {
        "platform:/plugin/x/icons/a.png": "invalid_ref",
        "HTTPS://x/A.png": "external",
        "../../../up.png": "above_root",
        "${IMG}.png": "invalid_ref",
    }
    for asset in out["assets"]:
        assert asset["path"] is None and asset["bytes"] is None
        _assert_entry_shape(asset)


async def test_space_in_name_is_copied(settings, converter, tmp_path):
    src = tmp_path / "src"
    _plant(src / "images" / "my tv.png", PNG)
    asset = _one(await _port(_panel(src, "images/my tv.png")))
    assert asset["status"] == "written" and asset["path"] == "screens/a/images/my tv.png"
    assert drafts.get_store(settings).resolve_static(asset["path"]) is not None


def _two_units_one_dest(tmp_path, first: bytes, second: bytes):
    """Parent ``a/top`` and sibling ``a/sib`` both naming ``images/x.png``, from two source trees."""
    top_dir, sib_dir = tmp_path / "t", tmp_path / "s"
    _plant(top_dir / "images" / "x.png", first)
    _plant(sib_dir / "images" / "x.png", second)
    body = picture("P", "images/x.png") + mocks.embedded("E", "sib.bob", 40)
    parent = _plant(top_dir / "top.bob", mocks.display(body).encode())
    sib = _panel(sib_dir, "images/x.png", name="sib.bob")
    return parent, {"sib.bob": str(sib)}


async def test_same_dest_with_differing_bytes_conflicts_on_first_port_and_re_port(settings, converter, tmp_path):
    parent, siblings = _two_units_one_dest(tmp_path, PNG, OTHER_PNG)
    out = await _port(parent, draft_id="a/top", siblings=siblings)
    assert [(a["draftId"], a["status"]) for a in out["assets"]] == [("a/top", "written"), ("a/sib", "conflict")]
    _assert_entry_shape(out["assets"][1])
    assert out["assets"][1]["bytes"] == len(OTHER_PNG)
    assert _asset_file(settings, "a/images/x.png").read_bytes() == PNG

    again = await _port(parent, draft_id="a/top", siblings=siblings, overwrite=True)
    assert [(a["draftId"], a["status"]) for a in again["assets"]] == [("a/top", "reused"), ("a/sib", "conflict")]
    assert _asset_file(settings, "a/images/x.png").read_bytes() == PNG


async def test_same_dest_with_equal_bytes_is_reused(settings, converter, tmp_path):
    parent, siblings = _two_units_one_dest(tmp_path, PNG, PNG)
    out = await _port(parent, draft_id="a/top", siblings=siblings)
    assert [a["status"] for a in out["assets"]] == ["written", "reused"]
    for asset in out["assets"]:
        _assert_entry_shape(asset)


async def test_rerun_reuses_assets_and_changed_bytes_need_overwrite(settings, converter, tmp_path):
    src = tmp_path / "src"
    image = _plant(src / "images" / "tv.png", PNG)
    panel = _panel(src, "images/tv.png")
    assert _one(await _port(panel))["status"] == "written"
    again = await _port(panel)
    assert _one(again)["status"] == "reused" and "refused" not in again

    image.write_bytes(OTHER_PNG)
    refused_ = _one(await _port(panel))
    assert refused_["status"] == "conflict" and "overwrite" in refused_["detail"]
    assert _asset_file(settings, "a/images/tv.png").read_bytes() == PNG

    replaced = _one(await _port(panel, overwrite=True))
    assert replaced["status"] == "replaced" and "detail" not in replaced
    assert replaced["bytes"] == len(OTHER_PNG)
    assert _asset_file(settings, "a/images/tv.png").read_bytes() == OTHER_PNG


async def test_draft_conflict_refuses_before_any_asset_work(settings, converter, tmp_path):
    src = tmp_path / "src"
    _plant(src / "images" / "tv.png", PNG)
    panel = _panel(src, "images/tv.png")
    await _port(panel)
    current = await get_screen("a/panel")
    node = current["screen"]["root"]["children"][0]["id"]
    await patch_node("a/panel", node, current["rev"], {"geometry": {"x": 5}})
    _asset_file(settings, "a/images/tv.png").unlink()
    out = await _port(panel)
    assert out["refused"] is True and "assets" not in out
    assert not _asset_file(settings, "a/images/tv.png").exists()


async def test_directory_at_the_source_is_missing(settings, converter, tmp_path):
    src = tmp_path / "src"
    (src / "images" / "x.png").mkdir(parents=True)
    asset = _one(await _port(_panel(src, "images/x.png")))
    assert asset["status"] == "missing" and asset["bytes"] is None
    assert asset["path"] == "screens/a/images/x.png"
    _assert_entry_shape(asset)


async def test_oversized_source_is_refused_before_reading(settings, converter, tmp_path, monkeypatch):
    src = tmp_path / "src"
    big = _plant(src / "images" / "big.png", PNG)
    with big.open("r+b") as fh:
        fh.truncate(MAX_ASSET_BYTES + 1)

    def no_open(*a, **k):
        raise AssertionError("an oversized source must not be read")

    monkeypatch.setattr(convert, "open", no_open, raising=False)
    asset = _one(await _port(_panel(src, "images/big.png")))
    assert asset["status"] == "too_large" and asset["bytes"] == MAX_ASSET_BYTES + 1
    _assert_entry_shape(asset)
    assert not _asset_file(settings, "a/images/big.png").exists()


async def test_source_growing_past_the_limit_is_too_large(settings, converter, tmp_path, monkeypatch):
    src = tmp_path / "src"
    _plant(src / "images" / "grow.png", PNG + b"\x00" * 1000)  # stat says ~1 KB

    def grown(path, mode="r", *a, **k):
        assert "b" in mode
        return io.BytesIO(PNG + b"\x00" * (MAX_ASSET_BYTES + 1))

    monkeypatch.setattr(convert, "open", grown, raising=False)
    asset = _one(await _port(_panel(src, "images/grow.png")))
    assert asset["status"] == "too_large"
    _assert_entry_shape(asset)
    assert not _asset_file(settings, "a/images/grow.png").exists()


async def test_symlink_to_a_non_image_is_not_published(settings, converter, tmp_path):
    src = tmp_path / "src"
    secret = _plant(tmp_path / "environ", b"VAR=1\x00OTHER=2\x00")
    (src / "images").mkdir(parents=True)
    os.symlink(secret, src / "images" / "tv.png")
    asset = _one(await _port(_panel(src, "images/tv.png")))
    assert asset["status"] == "not_image"
    assert asset["bytes"] == secret.stat().st_size
    _assert_entry_shape(asset)
    assert not _asset_file(settings, "a/images/tv.png").exists()


async def test_one_entry_per_image_ref_of_every_unit(settings, converter, tmp_path):
    src = tmp_path / "src"
    _plant(src / "images" / "a.png", PNG)
    out = await _port(_panel(src, "images/a.png", "images/a.png", "https://x/a.png"))
    assert [a["status"] for a in out["assets"]] == ["written", "reused", "external"]


def test_description_names_assets():
    doc = convert_legacy.__doc__ or ""
    assert "assets" in doc
    assert "no_source" in doc and "conflict" in doc
    assert "assets" in (convert.__doc__ or "")


# --- refs helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("embedded/_a.screen.json", "embedded/_a"),
        ("./embedded/./_a.screen.json", "embedded/_a"),
        ("x/../y.screen.json", "y"),
        ("../up.screen.json", "../up"),
        ("/abs/a.screen.json", None),
        ("~/a.screen.json", None),
        ("file:a.screen.json", None),
    ],
)
def test_ir_ref_key(raw, key):
    assert refs.ir_ref_key(raw) == key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("embedded/_a.bob", "embedded/_a"),
        ("embedded/_a.opi", "embedded/_a"),
        ("x.edl", "x"),
        ("./x.ui", "x"),
        ("x", "x"),
        ("/abs/x.bob", None),
        ("~x.bob", None),
        ("https://h/x.bob", None),
    ],
)
def test_sibling_key(raw, key):
    assert refs.sibling_key(raw) == key


def test_no_converted_als_content_is_checked_in():
    here = Path(__file__).parent / "fixtures" / "convert"
    assert {p.name for p in here.iterdir() if p.is_file()} <= {"mocks.py", "__init__.py"}
