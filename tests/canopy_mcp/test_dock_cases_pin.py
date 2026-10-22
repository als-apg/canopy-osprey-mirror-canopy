"""The copied C-CASES ``validate`` and ``normalize`` cases match their runtime pins.

``fixtures/dock-cases/`` carries runtime's ``validate`` and ``normalize`` cases byte for
byte (C-CASES). ``pin.json`` records, per copy, the runtime commit, the path in the
runtime repo and the git blob of the bytes. This test recomputes each copy's git blob,
so an edited, missing or extra copy fails here; the cross-repo gate recomputes the
blobs from runtime itself. Re-copy and re-pin, never edit a copy by hand.

The cases themselves are replayed against :func:`canopy_mcp.collections.layout_refusal`
and :func:`canopy_mcp.collections.normalize_layout` in ``test_collections_validate.py``.
"""

import re
import json
import hashlib
from pathlib import Path

import pytest

CASES = Path(__file__).parent / "fixtures" / "dock-cases"
PIN = CASES / "pin.json"
KINDS = ("validate", "normalize")
RUNTIME_DIR = "apps/viewer/src/workspace/dock/model/cases"


def git_blob(data: bytes) -> str:
    """The id ``git hash-object`` gives ``data``."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def pins() -> list[dict]:
    return json.loads(PIN.read_text(encoding="ascii"))["files"]


def copies() -> set[str]:
    return {f"{kind}/{path.name}" for kind in KINDS for path in (CASES / kind).iterdir()}


def test_git_blob_is_git_hash_object() -> None:
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_every_copy_is_pinned_and_every_pin_has_a_copy() -> None:
    pinned = [entry["copy"] for entry in pins()]
    assert len(pinned) == len(set(pinned))
    assert set(pinned) == copies()


def test_both_kinds_are_carried() -> None:
    for kind in KINDS:
        assert any(name.startswith(f"{kind}/") for name in copies()), kind


@pytest.mark.parametrize("entry", pins(), ids=lambda entry: entry["copy"])
def test_each_pin_names_a_runtime_commit_path_and_blob(entry: dict) -> None:
    assert set(entry) == {"copy", "commit", "path", "blob"}
    assert re.fullmatch(r"[0-9a-f]{40}", entry["commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", entry["blob"])
    assert entry["path"] == f"{RUNTIME_DIR}/{entry['copy']}"


@pytest.mark.parametrize("entry", pins(), ids=lambda entry: entry["copy"])
def test_each_copy_is_byte_identical_to_its_pin(entry: dict) -> None:
    assert git_blob((CASES / entry["copy"]).read_bytes()) == entry["blob"]


def test_the_pins_name_one_runtime_commit() -> None:
    assert len({entry["commit"] for entry in pins()}) == 1
