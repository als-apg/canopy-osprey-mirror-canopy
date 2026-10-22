"""The shared collections fixture set: valid/, invalid/ and library/.

Other suites copy these files byte for byte (the viewer's e2e fixtures, the OSPREY
template, the control-assistant deploy), so this test pins what each one means:
every valid file passes the validator and ``--check``, every invalid file is refused
by ``--check`` for exactly the rule its name says, the fixture library lists every
screen the valid set names, ``valid/list-body.json`` is the ``GET /collections``
body the real route serves for ``valid/``, and ``responses/list-refused.json`` is
that body with the ``old-shape/`` collections added to the personal store: each listed
as refused.

The personal valid set holds one collection per rule of the layout's collection form:
``grid`` (weights, nested splits), ``tabbed`` (several tabs per tile, ``showing``),
``root-tile`` (a root tile, ``showing`` left to default), ``deep`` (deeper than the
retired depth cap) and ``drafts`` (no layout). Every one places each member once.

Run with ``CANOPY_REGENERATE_FIXTURES=1`` to rewrite both list bodies.
"""

import os
import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.listing import scan, judge
from canopy_mcp.collections import (
    FACILITY,
    PERSONAL,
    OldShapeLayout,
    InvalidCollection,
    library_screens,
    validate_collection,
)
from canopy_mcp.collections_cli import EXIT_OK, EXIT_REFUSED, main

FIXTURES = Path(__file__).parent / "fixtures" / "collections"
VALID = FIXTURES / "valid"
FACILITY_DIR = VALID / "facility"
PERSONAL_DIR = VALID / "personal"
INVALID = FIXTURES / "invalid"
LIBRARY = FIXTURES / "library"
LIST_BODY = VALID / "list-body.json"
OLD_SHAPE = FIXTURES / "old-shape"
LIST_REFUSED = FIXTURES / "responses" / "list-refused.json"
FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"

# Each invalid file and a phrase of the one refusal --check gives it.
REFUSALS = {
    "bad.stem.json": "the file name is not a collection id",
    "not-a-file.json": "not a regular file",
    "not-utf8.json": "not UTF-8",
    "not-json.json": "not JSON",
    "not-object.json": "a collection must be an object",
    "unknown-key.json": "the collection has unknown key(s): colour",
    "id-bad.json": "id must match",
    "id-mismatch.json": "must equal the file id",
    "name-missing.json": "name is required",
    "name-empty.json": "name must be 1 to 120 characters",
    "name-too-long.json": "name must be 1 to 120 characters",
    "name-control-character.json": "control characters",
    "description-not-text.json": "description must be a string",
    "updated-not-text.json": "updated must be a string",
    "default-not-bool.json": "default must be true or false",
    "members-missing.json": "members is required",
    "members-not-array.json": "members must be an array",
    "members-too-many.json": "MAX_MEMBERS",
    "member-not-object.json": "members[0] must be an object",
    "member-both-kinds.json": "exactly one of screen and tool",
    "member-neither-kind.json": "exactly one of screen and tool",
    "member-unknown-key.json": "members[0] has unknown key(s): query",
    "screen-no-category.json": "must be '<category>/<id>'",
    "screen-bad-category.json": "category must be 'library' or 'drafts'",
    "screen-bad-id.json": "screen id must match",
    "screen-drafts-in-facility.json": "names library/* screens only",
    "screen-not-listed.json": "is not a screen the library lists",
    "macros-not-object.json": "macros must be an object",
    "macro-value-not-text.json": "must be a string",
    "tool-unknown.json": "tool must be one of",
    "tool-unnamed.json": "every tool member of a facility collection carries a name",
    "tool-query-too-long.json": "MAX_QUERY_BYTES",
    "tool-name-empty.json": "members[0].name",
    "member-duplicate-key.json": "duplicates member_key",
    "member-slot.json": "members[0].slot: old_slot_shape",
    "layout-old-slot-shape.json": "layout.items[0]: old_slot_shape",
    "layout-old-key-shape.json": "layout.items[0]: old_key_shape",
    "layout-not-object.json": "layout: not_layout",
    "layout-unknown-field.json": "layout.gap: unknown_field",
    "layout-tile-id.json": "layout.items[0].tile: unknown_field",
    "layout-from.json": "layout.items[1].from: unknown_field",
    "layout-bad-split.json": "layout.split: bad_split",
    "layout-one-item.json": "layout.items: split_too_few_items",
    "layout-empty-tabs.json": "layout.items[0].tabs: empty_tabs",
    "layout-bad-key.json": "layout.items[1].tabs[0]: bad_key",
    "layout-duplicate-key.json": "layout.items[1].tabs[0]: duplicate_key",
    "layout-showing-not-in-tabs.json": "layout.items[0].showing: showing_not_in_tabs",
    "layout-bad-size.json": "layout.items[0].size: bad_size",
    "layout-member-missing.json": "layout: member_missing",
}
# Refused before the validator sees a document (file name, file kind, decoding).
NOT_DOCUMENTS = {"bad.stem.json", "not-a-file.json", "not-utf8.json", "not-json.json"}
# The six D14b screens Task 5.4 checks against the control-assistant library.
D14B_SCREENS = {"gtb/GTLView", "bts/BTSView", "sr/overview", "s3-promo/promotion", "common/KnobPanel", "dev/b1-dev"}


def _docs(directory: Path) -> dict[str, object]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}


def _run(capsys, *argv) -> tuple[int, list[str], list[str]]:
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err.splitlines()


# --- the set itself ---------------------------------------------------------------


def test_the_set_has_the_files_the_plan_names():
    assert set(_docs(FACILITY_DIR)) == {
        "control-assistant.json",
        "injector.json",
        "storage-ring.json",
        "diagnostics.json",
    }
    assert set(_docs(PERSONAL_DIR)) == {"grid.json", "tabbed.json", "root-tile.json", "deep.json", "drafts.json"}
    assert set(_docs(OLD_SHAPE)) == {"old-grid.json", "old-undocked.json"}
    assert {p.name for p in INVALID.iterdir()} == set(REFUSALS)


def test_every_valid_file_carries_updated_so_the_list_body_is_stable():
    for directory in (FACILITY_DIR, PERSONAL_DIR, OLD_SHAPE):
        for name, doc in _docs(directory).items():
            assert isinstance(doc.get("updated"), str), name


def test_the_template_collection_is_d14a():
    doc = _docs(FACILITY_DIR)["control-assistant.json"]
    assert doc["default"] is True
    assert doc["members"] == [
        {"screen": "library/sr/overview"},
        {"tool": "trace", "name": "Trace"},
        {"tool": "correlation", "name": "Correlation"},
    ]


def test_the_deploy_trio_is_d14b():
    docs = _docs(FACILITY_DIR)
    shape = {
        "injector.json": ("Injector", ["library/gtb/GTLView", "library/bts/BTSView"], "trace"),
        "storage-ring.json": ("Storage ring", ["library/sr/overview", "library/s3-promo/promotion"], "correlation"),
        "diagnostics.json": ("Diagnostics", ["library/common/KnobPanel", "library/dev/b1-dev"], "waveform"),
    }
    for name, (title, screens, tool) in shape.items():
        doc = docs[name]
        assert doc["name"] == title
        assert "default" not in doc
        assert [m["screen"] for m in doc["members"] if "screen" in m] == screens
        tools = [m for m in doc["members"] if "tool" in m]
        assert len(tools) == 1 and tools[0]["tool"] == tool and tools[0]["name"]
    # Trace over PVs bound in GTLView, Correlation over two PVs bound in sr/overview,
    # Waveform at its config default.
    assert docs["injector.json"]["members"][2]["query"] == "pv=EG______HV_____AM00&pv=EG______HTR____AM00&range=-1h"
    assert (
        docs["storage-ring.json"]["members"][2]["query"]
        == "x=SR:DIAG:DCCT:01:CURRENT:RB&y=SR:RF:CAVITY:01:POWER:FWD&range=-1h"
    )
    assert "query" not in docs["diagnostics.json"]["members"][2]


def _tiles(node: dict) -> list[dict]:
    return [tile for item in node["items"] for tile in _tiles(item)] if "split" in node else [node]


def _depth(node: dict) -> int:
    return 1 + max(_depth(item) for item in node["items"]) if "split" in node else 0


def test_the_personal_set_has_one_collection_per_layout_rule():
    docs = _docs(PERSONAL_DIR)
    for name, doc in docs.items():
        if "layout" in doc:
            placed = [key for tile in _tiles(doc["layout"]) for key in tile["tabs"]]
            assert sorted(placed) == list(range(len(doc["members"]))), name
        assert all("slot" not in member for member in doc["members"]), name
    # Weights: two columns weighing 2 and 1, each a nested split.
    grid = docs["grid.json"]["layout"]
    assert [item["size"] for item in grid["items"]] == [2, 1]
    assert all(item["split"] == "column" and len(item["items"]) == 2 for item in grid["items"])
    # Several tabs in a tile, showing a tab other than the first.
    tabbed = _tiles(docs["tabbed.json"]["layout"])
    assert tabbed[0]["tabs"] == [0, 1] and tabbed[0]["showing"] == 1
    # A root tile, showing left to default.
    assert docs["root-tile.json"]["layout"] == {"tabs": [0, 1, 2]}
    # Deeper than the retired depth-6 cap.
    assert _depth(docs["deep.json"]["layout"]) > 6
    # No layout at all, and a draft member.
    assert "layout" not in docs["drafts.json"]
    assert any(m.get("screen", "").startswith("drafts/") for m in docs["drafts.json"]["members"])


def test_the_old_shape_set_is_refused_as_the_old_shape():
    for name, doc in _docs(OLD_SHAPE).items():
        with pytest.raises(OldShapeLayout):
            validate_collection(doc, source=PERSONAL, library=LIBRARY, file_id=name[: -len(".json")])


# --- library ----------------------------------------------------------------------


def test_the_library_lists_every_screen_the_valid_set_names_and_nothing_is_refused():
    judged = judge(scan(LIBRARY))
    assert judged.refusals == ()
    listed = library_screens(judged)
    named = {
        member["screen"].split("/", 1)[1]
        for directory in (FACILITY_DIR, PERSONAL_DIR, OLD_SHAPE)
        for doc in _docs(directory).values()
        for member in doc["members"]
        if member.get("screen", "").startswith("library/")
    }
    assert named <= listed
    assert D14B_SCREENS <= listed


# --- the validator ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_docs(FACILITY_DIR)))
def test_every_facility_file_passes_the_validator(name):
    doc = json.loads((FACILITY_DIR / name).read_text(encoding="utf-8"))
    validate_collection(doc, source=FACILITY, library=LIBRARY, file_id=name[: -len(".json")])


@pytest.mark.parametrize("name", sorted(_docs(PERSONAL_DIR)))
def test_every_personal_file_passes_the_validator(name):
    doc = json.loads((PERSONAL_DIR / name).read_text(encoding="utf-8"))
    clean = validate_collection(doc, source=PERSONAL, library=LIBRARY, file_id=name[: -len(".json")])
    assert clean["id"] == doc["id"]


@pytest.mark.parametrize("name", sorted(set(REFUSALS) - NOT_DOCUMENTS))
def test_every_invalid_document_is_refused_by_the_validator(name):
    doc = json.loads((INVALID / name).read_text(encoding="utf-8"))
    with pytest.raises(InvalidCollection) as exc:
        validate_collection(doc, source=FACILITY, library=LIBRARY, file_id=name[: -len(".json")])
    assert REFUSALS[name] in str(exc.value)


# --- --check ----------------------------------------------------------------------


def test_check_accepts_the_facility_set_against_the_fixture_library(capsys):
    code, out, err = _run(capsys, FACILITY_DIR, "--check", "--library", LIBRARY)
    assert (code, out, err) == (EXIT_OK, ["collections: 4 checked, 0 refused"], [])


def test_check_refuses_every_invalid_file_once_for_its_own_rule(capsys):
    code, out, err = _run(capsys, INVALID, "--check", "--library", LIBRARY)
    assert code == EXIT_REFUSED
    assert out == [f"collections: {len(REFUSALS)} checked, {len(REFUSALS)} refused"]
    assert len(err) == len(REFUSALS)
    by_file = {}
    for line in err:
        prefix = "canopy_mcp.collections: refused: "
        assert line.startswith(prefix), line
        name, _, reason = line[len(prefix) :].partition(": ")
        by_file[name] = reason
    assert set(by_file) == set(REFUSALS)
    for name, phrase in REFUSALS.items():
        assert phrase in by_file[name], (name, by_file[name])


def test_without_a_library_only_the_unlisted_screen_passes(capsys):
    code, out, err = _run(capsys, INVALID, "--check")
    assert code == EXIT_REFUSED
    refused = {line.split(": ")[2] for line in err}
    assert set(REFUSALS) - refused == {"screen-not-listed.json"}


# --- GET /collections ------------------------------------------------------------


def _list_body(tmp_path: Path, *extra: Path) -> dict:
    personal = tmp_path / "personal"
    shutil.copytree(PERSONAL_DIR, personal)
    for directory in extra:
        for path in directory.glob("*.json"):
            shutil.copy(path, personal / path.name)
    drafts = tmp_path / "drafts"
    (drafts / "screens").mkdir(parents=True)
    settings = Settings(
        drafts_dir=drafts,
        viewer_dist=FIXTURE_DIST,
        facility_collections_dir=FACILITY_DIR,
        collections_dir=personal,
    )
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        response = client.get("/collections")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    return response.json()


def test_the_list_body_is_what_the_route_serves_for_valid(tmp_path):
    body = _list_body(tmp_path)
    if os.environ.get("CANOPY_REGENERATE_FIXTURES"):
        LIST_BODY.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(LIST_BODY.read_text(encoding="utf-8")) == body
    refs = [doc["ref"] for doc in body["collections"]]
    assert refs == [
        "facility:control-assistant",
        "facility:diagnostics",
        "facility:injector",
        "facility:storage-ring",
        "personal:deep",
        "personal:drafts",
        "personal:root-tile",
        "personal:grid",
        "personal:tabbed",
    ]
    assert all("refused" not in doc for doc in body["collections"])


def test_the_refused_list_body_lists_the_old_shape_as_refused(tmp_path):
    body = _list_body(tmp_path, OLD_SHAPE)
    if os.environ.get("CANOPY_REGENERATE_FIXTURES"):
        LIST_REFUSED.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(LIST_REFUSED.read_text(encoding="utf-8")) == body
    refused = {doc["ref"]: doc for doc in body["collections"] if "refused" in doc}
    assert set(refused) == {"personal:old-grid", "personal:old-undocked"}
    for doc in refused.values():
        assert set(doc) == {"source", "ref", "rev", "id", "name", "members", "refused"}
        assert "old_slot_shape" in doc["refused"]
        assert all("slot" not in member for member in doc["members"])
    assert refused["personal:old-grid"]["members"][0] == {"screen": "library/sr/overview"}
    # The valid collections are listed exactly as without the old ones.
    valid = [doc for doc in body["collections"] if "refused" not in doc]
    assert valid == _list_body(tmp_path / "again")["collections"]
