"""A library's ``navigation.json``: what is accepted, what is ignored, and how the manifest carries it.

``canopy_mcp.navigation`` checks the shape and the limits of the file and hands a
valid one to the viewer unchanged. These tests hold it to three things:

* a valid file is in ``GET /drafts/manifest.json`` as ``navigation``, and a missing
  or invalid one leaves the key out and the screens as they were;
* an invalid file is warned about once for each state of the file, not once per poll;
* an unchanged file is not opened again, and a changed one shows on the next request.
"""

import os
import json
import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.navigation import (
    NAVIGATION_FILE,
    MAX_NAVIGATION_BYTES,
    MAX_NAVIGATION_DEPTH,
    MAX_NAVIGATION_LABEL,
    MAX_NAVIGATION_ENTRIES,
    NavigationFile,
    InvalidNavigation,
    check_navigation,
)

VALID = [
    {"screen": "library/overview", "label": "Start here"},
    {
        "label": "Pumps",
        "screen": "library/pumps/all",
        "children": [
            {"screen": "library/pumps/p1"},
            {"label": "Spares", "children": [{"screen": "library/pumps/spare"}]},
            {"label": "Empty", "children": []},
        ],
    },
]


def _nested(depth: int) -> list:
    entries: list = [{"screen": "library/overview"}]
    for _ in range(depth - 1):
        entries = [{"label": "Group", "children": entries}]
    return entries


def _write(root: Path, value: object) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / NAVIGATION_FILE
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "canopy_mcp.navigation"]


# --- the shape --------------------------------------------------------------


def test_a_valid_navigation_is_returned_unchanged():
    assert check_navigation(VALID) is VALID
    assert check_navigation([]) == []
    assert check_navigation(_nested(MAX_NAVIGATION_DEPTH))


@pytest.mark.parametrize(
    ("entries", "fault"),
    [
        ({"screen": "library/overview"}, "expected a list"),
        (["library/overview"], "expected an object"),
        ([{"screen": "library/overview", "title": "x"}], "unknown key 'title'"),
        ([{"screen": ""}], "screen: expected non-empty text"),
        ([{"screen": 3}], "screen: expected non-empty text"),
        ([{"screen": "library/overview", "label": "  "}], "label: expected non-empty text"),
        ([{"screen": "library/overview", "label": "x" * (MAX_NAVIGATION_LABEL + 1)}], "label: longer than"),
        ([{"label": "Only a name"}], "needs a screen or children"),
        ([{"children": [], "screen": "library/overview"}], "a group needs a label"),
        ([{"label": "Pumps", "children": {"screen": "library/pumps/p1"}}], "children: expected a list"),
        ([{"label": "Pumps", "children": [{"label": "Inner"}]}], "navigation[0].children[0]: needs a screen"),
        (_nested(MAX_NAVIGATION_DEPTH + 1), "nested deeper than"),
        ([{"screen": "library/overview"}] * (MAX_NAVIGATION_ENTRIES + 1), "more than"),
    ],
)
def test_an_invalid_navigation_names_its_first_fault(entries, fault):
    with pytest.raises(InvalidNavigation) as raised:
        check_navigation(entries)
    assert fault in str(raised.value)


# --- the file ---------------------------------------------------------------


def test_a_missing_file_is_no_navigation_and_no_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert NavigationFile(tmp_path).read() is None
        assert NavigationFile(tmp_path / "absent").read() is None
    assert _warnings(caplog) == []


def test_a_valid_file_is_read(tmp_path):
    _write(tmp_path, VALID)
    assert NavigationFile(tmp_path).read() == VALID


@pytest.mark.parametrize(
    ("text", "fault"),
    [
        ("{not json", "not JSON"),
        (json.dumps([{"label": "Only a name"}]), "needs a screen or children"),
        (json.dumps(VALID) + " " * MAX_NAVIGATION_BYTES, "byte limit"),
    ],
)
def test_an_invalid_file_is_ignored_with_one_warning_per_state(tmp_path, caplog, text, fault):
    path = _write(tmp_path, text)
    source = NavigationFile(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert source.read() is None
        assert source.read() is None
        assert source.read() is None
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert fault in warned[0] and str(path.resolve()) in warned[0]

    # Another invalid state of the file is another warning; a repair is none.
    caplog.clear()
    _write(tmp_path, "[1]")
    with caplog.at_level(logging.WARNING):
        assert source.read() is None
        assert source.read() is None
    assert len(_warnings(caplog)) == 1
    caplog.clear()
    _write(tmp_path, VALID)
    with caplog.at_level(logging.WARNING):
        assert source.read() == VALID
    assert _warnings(caplog) == []


def test_a_link_out_of_the_root_is_ignored_with_one_warning(tmp_path, caplog):
    outside = _write(tmp_path / "elsewhere", VALID)
    root = tmp_path / "library"
    root.mkdir()
    (root / NAVIGATION_FILE).symlink_to(outside)
    source = NavigationFile(root)
    with caplog.at_level(logging.WARNING):
        assert source.read() is None
        assert source.read() is None
    warned = _warnings(caplog)
    assert len(warned) == 1 and "inside the library root" in warned[0]


def test_an_unchanged_file_is_not_opened_again(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    source = NavigationFile(tmp_path)
    assert source.read() == VALID

    opened = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    assert source.read() == VALID
    assert opened == []

    changed = [{"screen": "library/other"}]
    _write(tmp_path, changed)
    os.utime(path, ns=(1, 1))  # a new stat key, whatever the clock's resolution
    opened.clear()  # the write above went through the same call
    assert source.read() == changed
    assert opened == [NAVIGATION_FILE]

    path.unlink()
    assert source.read() is None


# --- the manifest -----------------------------------------------------------


@pytest.fixture
def served(tmp_path):
    """A client over a personal store and a library of one screen, and the library root."""
    library = tmp_path / "library"
    screen = library / "screens" / "overview.screen.json"
    screen.parent.mkdir(parents=True)
    screen.write_text(json.dumps({"metadata": {"title": "Overview"}}), encoding="utf-8")
    settings = Settings(drafts_dir=tmp_path / "drafts", library_dir=library, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        yield client, library


def test_the_manifest_carries_a_valid_navigation(served):
    client, library = served
    plain = client.get("/drafts/manifest.json")
    assert "navigation" not in plain.json()

    _write(library, VALID)
    with_navigation = client.get("/drafts/manifest.json")
    assert with_navigation.json() == {**plain.json(), "navigation": VALID}
    assert with_navigation.headers["etag"] != plain.headers["etag"]
    again = client.get("/drafts/manifest.json", headers={"If-None-Match": with_navigation.headers["etag"]})
    assert again.status_code == 304

    (library / NAVIGATION_FILE).unlink()
    assert client.get("/drafts/manifest.json").json() == plain.json()


def test_the_manifest_ignores_an_invalid_navigation(served, caplog):
    client, library = served
    plain = client.get("/drafts/manifest.json").json()
    _write(library, [{"label": "Only a name"}])
    with caplog.at_level(logging.WARNING):
        assert client.get("/drafts/manifest.json").json() == plain
        assert client.get("/drafts/manifest.json").json() == plain
    assert len(_warnings(caplog)) == 1


def test_navigation_is_read_at_the_library_root_only(tmp_path):
    drafts = tmp_path / "drafts"
    _write(drafts, VALID)
    with TestClient(create_app(Settings(drafts_dir=drafts, pv_mode="hare", api_key="k"), startup_hooks=[])) as client:
        assert "navigation" not in client.get("/drafts/manifest.json").json()


def test_the_navigation_file_is_not_served(served):
    client, library = served
    _write(library, VALID)
    assert client.get(f"/drafts/library/{NAVIGATION_FILE}").status_code == 404
