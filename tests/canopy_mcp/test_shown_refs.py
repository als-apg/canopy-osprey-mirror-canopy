"""shown_refs replays the shared ``collections-shown`` vector the viewer replays too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from canopy_mcp.collections import shown_refs

VECTOR = Path(__file__).parent / "fixtures" / "workspace" / "collections-shown.json"
CASES = json.loads(VECTOR.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shown_refs_vector(case):
    assert shown_refs(case["collections"], case["state"]) == case["expect"]


def test_shown_refs_vector_is_not_empty():
    assert len(CASES) >= 5


def test_shown_refs_missing_default_is_not_shown():
    # A personal collection, or a facility one that leaves ``default`` out.
    docs = [{"ref": "facility:a"}, {"ref": "personal:b", "source": "personal"}]
    assert shown_refs(docs, {"ticked": [], "unticked": []}) == []


def test_shown_refs_only_true_counts_as_default():
    docs = [{"ref": "facility:a", "default": "yes"}, {"ref": "facility:b", "default": 1}]
    assert shown_refs(docs, {"ticked": [], "unticked": []}) == []


def test_shown_refs_accepts_full_state_and_missing_lists():
    docs = [{"ref": "facility:a", "default": True}, {"ref": "personal:b"}]
    full = {"ticked": ["personal:b"], "unticked": [], "pinned": [{"tool": "trace"}], "recent": []}
    assert shown_refs(docs, full) == ["facility:a", "personal:b"]
    assert shown_refs(docs, {}) == ["facility:a"]


def test_shown_refs_does_not_mutate_inputs():
    docs = [{"ref": "facility:a", "default": True}]
    state = {"ticked": ["facility:a"], "unticked": []}
    before = json.dumps([docs, state])
    shown_refs(docs, state)
    assert json.dumps([docs, state]) == before


def test_shown_refs_with_all_collections_docs(tmp_path):
    from canopy_mcp.collections import FacilityCollections, all_collections

    facility_dir = tmp_path / "facility"
    facility_dir.mkdir()
    (facility_dir / "ring.json").write_text(
        json.dumps({"name": "Ring", "members": [], "default": True}), encoding="utf-8"
    )
    (facility_dir / "inj.json").write_text(json.dumps({"name": "Injector", "members": []}), encoding="utf-8")
    docs = all_collections(FacilityCollections(facility_dir), None)
    assert {doc["ref"] for doc in docs} == {"facility:ring", "facility:inj"}
    assert shown_refs(docs, {"ticked": ["facility:inj"], "unticked": ["facility:ring"]}) == ["facility:inj"]
