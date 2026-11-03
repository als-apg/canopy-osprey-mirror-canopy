"""``canopy_mcp.report``: the report document, its markdown, and the ``--emit`` / ``--write`` / ``--check`` command.

``fixtures/report-golden.json`` holds the report over the trees under
``fixtures/report-tree/``, keyed by ``generator.schema``. A change to any row
must bump the schema, or the golden test fails. To rewrite the golden after
such a bump: ``python tests/canopy_mcp/test_report.py``.
"""

from __future__ import annotations

import sys
import json
import shutil
import subprocess
from typing import Any
from pathlib import Path

import pytest

from canopy_mcp import report, listing
from canopy_mcp.store import lock_root

FIXTURES = Path(__file__).parent / "fixtures"
TREES = FIXTURES / "report-tree"
GOLDEN_PATH = FIXTURES / "report-golden.json"
INVALID_TEXT = "invalid_text: screens/bad.screen.json /root/children/0/props/text"

ABOUT = [
    "The canopy_mcp.report document over the trees under tests/canopy_mcp/fixtures/report-tree/, keyed by generator.schema.",
    "A change to any row output must bump the schema, or the golden test fails. generator.canopy_version is left out: it is not compared.",
    "library: served as a library; nothing is refused. md holds the lines of LIBRARY-REPORT.md.",
    "library holds the file top/Embedded/_X.part.screen.json and a ref embedded/_X.part.screen.json: the ref is unresolved on every platform, so the write binding PART:X is in no row.",
    "refused: the lines --check refuses the tree with. invalid-text: the refusal every mode gives.",
    "ASCII-only, no trailing whitespace, one final newline.",
]


def _without_version(document: dict[str, Any]) -> dict[str, Any]:
    out = dict(document)
    out["generator"] = {key: value for key, value in document["generator"].items() if key != "canopy_version"}
    return out


def _case(tree: str, *, md: bool) -> dict[str, Any]:
    built = report._build(TREES / tree, "library")
    case: dict[str, Any] = {"report": _without_version(built.report), "refusals": report._refusals(built)}
    if md:
        case["md"] = report.render_markdown(built.report).split("\n")
    return case


def build_golden() -> dict[str, Any]:
    with pytest.raises(report.ReportRefused) as refused:
        report.report_for_tree(TREES / "invalid-text", served_at="library")
    return {
        "about": ABOUT,
        report.SCHEMA: {
            "library": _case("library", md=True),
            "refused": _case("refused", md=False),
            "invalid-text": {"refused": str(refused.value)},
        },
    }


def golden_bytes() -> bytes:
    return (json.dumps(build_golden(), indent=2, ensure_ascii=True) + "\n").encode("ascii")


if __name__ == "__main__":
    GOLDEN_PATH.write_bytes(golden_bytes())
    sys.exit(0)

GOLDEN_BYTES = GOLDEN_PATH.read_bytes()
GOLDEN = json.loads(GOLDEN_BYTES)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A writable copy of the clean fixture tree."""
    root = tmp_path / "library"
    shutil.copytree(TREES / "library", root)
    return root


def _run(*argv: str) -> int:
    return report.main(list(argv))


def _screen(*children: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "s",
        "metadata": {"title": "s", "source": {"type": "hand-authored"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": {"id": "root", "type": "absolute-canvas", "props": {}, "children": list(children)},
    }


def _put(root: Path, key: str, doc: Any) -> None:
    file = root / "screens" / f"{key}.screen.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(doc), encoding="utf-8")


def _clean_bytes(data: bytes) -> bool:
    """Exactly one final newline and no trailing whitespace on any line."""
    return (
        data.endswith(b"\n")
        and not data.endswith(b"\n\n")
        and b"\r" not in data
        and all(line == line.rstrip() for line in data.split(b"\n"))
    )


# --- the golden --------------------------------------------------------------------------


def test_the_golden_is_keyed_by_the_schema_and_holds_no_version() -> None:
    assert set(GOLDEN) == {"about", report.SCHEMA} and report.SCHEMA == "canopy-library-report/1"
    for case in ("library", "refused"):
        assert GOLDEN[report.SCHEMA][case]["report"]["generator"] == {"schema": report.SCHEMA}
    assert b"canopy_version" not in GOLDEN_BYTES.replace(b"generator.canopy_version", b"")


def test_the_report_over_the_fixture_trees_equals_the_golden() -> None:
    assert build_golden() == GOLDEN
    assert golden_bytes() == GOLDEN_BYTES


def test_the_golden_and_the_vector_file_are_clean_ascii() -> None:
    for data in (GOLDEN_BYTES, (FIXTURES / "listing-vectors.json").read_bytes()):
        assert data.isascii() and b"\t" not in data and _clean_bytes(data)


def test_the_document_has_the_schema_keys_in_order() -> None:
    document = report.report_for_tree(TREES / "library", served_at="library")
    assert list(document) == ["generator", "files", *report.ROW_KEYS]
    assert set(document["generator"]) == {"schema", "canopy_version"}
    assert isinstance(document["generator"]["canopy_version"], str) and document["generator"]["canopy_version"]
    assert list(document["files"]) == ["digest", "list"]
    assert "provenance" not in document
    assert not any("digest" in key.lower() for key in report.ROW_KEYS)


def test_the_files_are_the_judged_listing() -> None:
    document = report.report_for_tree(TREES / "library", served_at="library")
    judged = listing.judge(listing.scan(TREES / "library"))
    assert document["files"]["digest"] == judged.digest
    assert [(f["path"], f["sha256"]) for f in document["files"]["list"]] == list(judged.files)
    assert [f["path"] for f in document["files"]["list"] if f["path"].endswith(".png")] == ["screens/top/logo.png"]


def test_a_case_mismatched_embed_is_unresolved_and_its_write_binding_is_in_no_row() -> None:
    """File ``top/Embedded/_X.part``, ref ``embedded/_X.part``: this host would open the file; the listing does not."""
    assert (TREES / "library/screens/top/Embedded/_X.part.screen.json").is_file()
    document = GOLDEN[report.SCHEMA]["library"]["report"]
    main = next(screen for screen in document["screens"] if screen["id"] == "top/Main")
    assert [row["instance"] for row in main["instances"]] == ["", "/0:part", "/h:under"]
    assert {"kind": "embed", "screen": "top/Main", "instance": "", "nodeId": "folded"}.items() <= next(
        row for row in main["unresolvedRefs"] if row["nodeId"] == "folded"
    ).items()
    assert "PART:X" not in json.dumps(document["writeBindings"])
    assert "PART:X" not in json.dumps(document)
    template = next(t for t in document["templates"] if t["path"].endswith("_X.part.screen.json"))
    assert template["role"] == "unlisted"


def test_every_walk_gets_the_judged_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    judged = listing.judge(listing.scan(TREES / "library")).paths
    seen: list[Any] = []

    def spy(name: str, target: Any) -> None:
        real = getattr(target, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            seen.append((name, kwargs.get("listing")))
            return real(*args, **kwargs)

        monkeypatch.setattr(target, name, call)

    spy("expand", report.screens)
    spy("list_screen_write_bindings", report.library_screens)
    spy("check_refs", report)
    spy("resolve", report)
    report.report_for_tree(TREES / "library", served_at="library")
    names = {name for name, _listing in seen}
    assert names == {"expand", "list_screen_write_bindings", "check_refs", "resolve"}
    # An image ref is classed as external or not without asking for existence at all.
    assert all(given == judged for name, given in seen if name != "resolve")
    assert {given for name, given in seen if name == "resolve"} <= {judged, None}
    assert judged in {given for name, given in seen if name == "resolve"}


def test_rows_of_the_clean_tree() -> None:
    document = GOLDEN[report.SCHEMA]["library"]["report"]
    assert document["manifest"] == ["array", "broken", "top/Main", "top/other"]
    assert {t["screen"]: t["role"] for t in document["templates"]} == {
        "array": "manifest",
        "broken": "manifest",
        "odd/v1.2": "unlisted",
        "top/Embedded/_X.part": "unlisted",
        "top/Main": "manifest",
        "top/other": "manifest",
        "top/parts/part.v2": "embedded",
    }
    assert [(s["id"], s.get("error")) for s in document["screens"]] == [
        ("array", "not a JSON object"),
        ("broken", "not valid JSON"),
        ("top/Main", None),
        ("top/other", None),
    ]
    assert all(row["top"] == "top/Main" for row in document["writeBindings"])
    assert [row["pvResolved"] for row in document["writeBindings"]] == [
        "SR01:SET",
        "SR01:IN",
        "loc://zero",
        "SR01:ZERO",
    ]
    assert [(row["nodeId"], row["hidden"]) for row in document["executeActions"]] == [("run", False), ("shell", True)]
    assert document["executeActions"][0] == {
        "topScreen": "top/Main",
        "screen": "top/Main",
        "instance": "",
        "nodeId": "run",
        "nodeType": "regular-button",
        "command": "Run | thing",
        "description": "starts *it*",
        "hidden": False,
    }
    assert [(row["nodeId"], row["hidden"]) for row in document["commandButtons"]] == [("run", False), ("shell", True)]
    assert [(row["instance"], row["file"], row["target"]) for row in document["openDisplays"]] == [
        ("", "other.screen.json", "top/other"),
        ("", "parts/part.v2.screen.json", None),
        ("", "gone.screen.json", None),
        ("/h:under", "Main.screen.json", "top/Main"),
        ("", "Main.screen.json", "top/Main"),
    ]
    assert [(row["kind"], row["url"]) for row in document["absoluteUrls"]] == [
        ("open_webpage", "https://example.org/a_b"),
        ("open_webpage", "/docs/x"),
        ("image", "https://example.org/logo.png"),
        ("string", "HTTP://example.org/Help"),
    ]
    main = next(screen for screen in document["screens"] if screen["id"] == "top/Main")
    assert [m["name"] for m in main["urlMacros"]] == ["DEV", "P", "UNSET"]  # ``theme`` is the viewer's own
    assert [(r["kind"], r["nodeId"], r["file"]) for r in main["unresolvedRefs"]] == [
        ("open_display", "run", "parts/part.v2.screen.json"),
        ("open_display", "run", "gone.screen.json"),
        ("embed", "folded", "embedded/_X.part.screen.json"),
        ("embed", "missing", "nope.screen.json"),
    ]
    assert main["unresolvedMacros"] == [{"screen": "top/Main", "instance": "", "nodeId": "loose"}]
    for key in report.REFUSED_ROW_KEYS:
        assert document[key] == []
    assert GOLDEN[report.SCHEMA]["library"]["refusals"] == []


def test_rows_and_refusals_of_the_refused_tree() -> None:
    case = GOLDEN[report.SCHEMA]["refused"]
    document = case["report"]
    assert [(row["nodeId"], row["kind"], row["ref"]) for row in document["crossRootRefs"]] == [
        ("cross", "screen", "../../../screens/p.screen.json")
    ]
    assert [(row["nodeId"], row["ref"]) for row in document["rootRelativeRefs"]] == [
        ("rooted", "/a/other.screen.json"),
        ("prefixed", "screens/x.screen.json"),
    ]
    assert [row["targetProperty"] for row in document["ruleRetargetedChannels"]] == ["pv"]
    assert [row["targetProperty"] for row in document["ruleRetargetedRefs"]] == ["actions[0].file"]
    assert document["ruleRetargetedRefs"][0] == {
        "screen": "a/top",
        "nodeId": "ruled",
        "nodeType": "pv-label",
        "path": "/root/children/4/rules/1/targetProperty",
        "targetProperty": "actions[0].file",
    }
    assert [line.split(":", 1)[0] for line in case["refusals"]] == [
        "outside_screens",
        "suffix",
        "svg",
        "crossRootRefs",
        "rootRelativeRefs",
        "rootRelativeRefs",
        "ruleRetargetedChannels",
        "ruleRetargetedRefs",
    ]
    assert case["refusals"][0].startswith("outside_screens: README.md: ")
    assert case["refusals"][1].startswith("suffix: screens/notes.txt: ")
    assert case["refusals"][2].startswith("svg: screens/a/icon.svg: ")
    assert all(line.isascii() and "\n" not in line for line in case["refusals"])


def test_root_relative_refs_are_rows_only_for_a_library() -> None:
    personal = report.report_for_tree(TREES / "refused", served_at="personal")
    assert personal["rootRelativeRefs"] == []
    # Served at ``screens/``, the same ref lands above the root instead of in the other one.
    assert personal["crossRootRefs"] == []
    served = report.report_for_tree(TREES / "refused", served_at="library")
    assert len(served["rootRelativeRefs"]) == 2 and len(served["crossRootRefs"]) == 1


def test_a_personal_ref_into_the_library_is_a_cross_root_row(tmp_path: Path) -> None:
    embed = {"id": "e", "type": "embedded-display", "props": {"file": "../library/screens/x.screen.json"}}
    _put(tmp_path, "top", _screen(embed))
    rows = report.report_for_tree(tmp_path, served_at="personal")["crossRootRefs"]
    assert rows == [
        {
            "topScreen": "top",
            "screen": "top",
            "nodeId": "e",
            "kind": "screen",
            "path": "/root/children/0/props/file",
            "ref": "../library/screens/x.screen.json",
        }
    ]


def test_a_ref_row_of_an_embedded_document_names_that_document(tmp_path: Path) -> None:
    opens = {"type": "open_display", "file": "/abs.screen.json"}
    _put(tmp_path, "a/part", _screen({"id": "b", "type": "regular-button", "props": {"actions": [opens]}}))
    _put(tmp_path, "a/top", _screen({"id": "e", "type": "embedded-display", "props": {"file": "part.screen.json"}}))
    rows = report.report_for_tree(tmp_path, served_at="library")["rootRelativeRefs"]
    assert [(row["topScreen"], row["screen"], row["nodeId"], row["ref"]) for row in rows] == [
        ("a/part", "a/part", "b", "/abs.screen.json"),
        ("a/top", "a/part", "b", "/abs.screen.json"),
    ]


def test_an_embed_under_a_hidden_embed_is_not_called_unresolved(tmp_path: Path) -> None:
    _put(tmp_path, "leaf", _screen({"id": "s", "type": "pv-slider", "props": {"pv": "LEAF"}}))
    _put(tmp_path, "mid", _screen({"id": "in", "type": "embedded-display", "props": {"file": "leaf.screen.json"}}))
    _put(
        tmp_path,
        "top",
        _screen(
            {"id": "E1", "type": "embedded-display", "props": {"file": "mid.screen.json", "visible": False}},
            {"id": "E3", "type": "embedded-display", "props": {"file": "leaf.screen.json"}},
            {"id": "E4", "type": "embedded-display", "props": {"file": "absent.screen.json"}},
        ),
    )
    top = next(s for s in report.report_for_tree(tmp_path, served_at="library")["screens"] if s["id"] == "top")
    assert top["instances"] == [
        {"instance": "", "screen": "top", "hidden": False},
        {"instance": "/0:E1", "screen": "mid", "hidden": True},
        {"instance": "/0:E1/0:in", "screen": "leaf", "hidden": True},
        {"instance": "/1:E3", "screen": "leaf", "hidden": False},
    ]
    assert [row["nodeId"] for row in top["unresolvedRefs"]] == ["E4"]


def test_an_error_text_does_not_depend_on_the_parser(tmp_path: Path) -> None:
    (tmp_path / "screens").mkdir()
    (tmp_path / "screens" / "bad.screen.json").write_bytes(b"\xff\xfe{")
    (tmp_path / "screens" / "deep.screen.json").write_bytes(b"[" * 100_000)
    document = report.report_for_tree(tmp_path, served_at="library")
    assert [s["error"] for s in document["screens"]] == ["not valid JSON", "not valid JSON"]
    assert [t["error"] for t in document["templates"]] == ["not valid JSON", "not valid JSON"]


# --- invalid text ------------------------------------------------------------------------


def test_a_forbidden_character_refuses_before_any_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    assert GOLDEN[report.SCHEMA]["invalid-text"] == {"refused": INVALID_TEXT}

    def no_digest(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the listing was judged before the text was checked")

    monkeypatch.setattr(report, "judge", no_digest)
    with pytest.raises(report.ReportRefused, match="^invalid_text: "):
        report.report_for_tree(TREES / "invalid-text", served_at="library")


@pytest.mark.parametrize("mode", ["--emit", "--write", "--check"])
def test_every_mode_refuses_invalid_text(tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str) -> None:
    root = tmp_path / "library"
    shutil.copytree(TREES / "invalid-text", root)
    assert _run(str(root), mode) == report.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"canopy_mcp.report: refused: {INVALID_TEXT}\n"
    assert sorted(path.name for path in root.iterdir()) == ["screens"]


def test_a_forbidden_object_key_reports_the_object(tmp_path: Path) -> None:
    _put(tmp_path, "top", _screen({"id": "n", "type": "text-label", "props": {"a​b": "x"}}))
    with pytest.raises(report.ReportRefused) as refused:
        report.report_for_tree(tmp_path, served_at="library")
    assert str(refused.value) == "invalid_text: screens/top.screen.json /root/children/0/props"


# --- safe_cell and the markdown ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        (None, ""),
        (True, "yes"),
        (False, "no"),
        (7, "7"),
        ("plain text", "plain text"),
        ("a|b", "a\\|b"),
        ("a\\b", "a\\\\b"),
        ("a\\|b", "a\\\\\\|b"),
        ("one\ntwo\r\nthree\tfour", "one two  three four"),
        ("<b>[x](y)`z`*w*_v_", "\\<b\\>\\[x\\](y)\\`z\\`\\*w\\*\\_v\\_"),
        ("café", "caf\\u00e9"),
        ("\U0001f600", "\\ud83d\\ude00"),
        ("a‮b\x00c\x1bd\x7fe\x85f​g﻿h", "abcdefgh"),
        ("lone\udc80", "lone"),
        ("\\u00e9", "\\\\u00e9"),
    ],
)
def test_safe_cell(value: Any, cell: str) -> None:
    assert report.safe_cell(value) == cell
    assert report.safe_cell(value).isascii()


def test_the_markdown_reads_the_schema_the_files_and_the_rows_alone() -> None:
    document = report.report_for_tree(TREES / "library", served_at="library")
    plain = report.render_markdown(document)
    document["provenance"] = {"snapshot": {"path": "/somewhere", "sha256": "0" * 64}, "wheels": {"canopy": "9.9"}}
    document["generator"]["canopy_version"] = "9.9.9"
    assert report.render_markdown(document) == plain
    assert "9.9" not in plain and "somewhere" not in plain
    assert plain.split("\n") == GOLDEN[report.SCHEMA]["library"]["md"]


def test_the_markdown_lays_the_tables_out_as_the_ported_report_did() -> None:
    lines = GOLDEN[report.SCHEMA]["library"]["md"]
    assert lines[0] == "# Report of the screen library"
    headings = [line for line in lines if line.startswith("## ")]
    assert headings == [
        "## Sources",
        "## Write bindings (4)",
        "## Execute actions (2)",
        "## Command buttons (2)",
        "## Open displays (5)",
        "## Absolute URLs (4)",
        "## Cross-root refs (0)",
        "## Root-relative refs (0)",
        "## Rule-retargeted channels (0)",
        "## Rule-retargeted refs (0)",
        "## Top-level screens",
        "## Templates",
        "## Files",
    ]
    at = lines.index("## Write bindings (4)")
    assert lines[at + 2] == (
        "| top screen | screen | instance | node | kind | PV as written | PV resolved | local | unresolved macro |"
    )
    assert lines[at + 3] == "|" + "---|" * 9
    assert lines[at + 4] == "| top/Main | top/Main | (top) | slider | pv-slider | ${DEV}:SET | SR01:SET | no | no |"
    assert "| top/Main | top/Main | (top) | run | regular-button | Run \\| thing | starts \\*it\\* | no |" in lines
    assert lines[lines.index("## Cross-root refs (0)") + 2] == "none"
    assert "- not readable: not valid JSON" in lines


def test_a_hostile_string_cannot_leave_its_cell(tmp_path: Path) -> None:
    command = "x |\n| a | b |\n## Write bindings (0)\n<script>alert(1)</script> café"
    button = {
        "id": "run*me*",
        "type": "regular-button",
        "props": {"actions": [{"type": "execute", "command": command}]},
    }
    _put(tmp_path, "top", _screen(button))
    markdown = report.render_markdown(report.report_for_tree(tmp_path, served_at="library"))
    assert markdown.isascii() and _clean_bytes(markdown.encode())
    row = next(line for line in markdown.split("\n") if "alert" in line)
    assert row == (
        "| top | top | (top) | run\\*me\\* | regular-button | "
        "x \\| \\| a \\| b \\| ## Write bindings (0) \\<script\\>alert(1)\\</script\\> caf\\u00e9 |  | no |"
    )
    assert markdown.count("## Write bindings (") == 2  # the heading, and the one inside the cell


# --- the command -------------------------------------------------------------------------


def test_served_at_defaults_to_library_and_one_mode_is_required() -> None:
    assert report.parse_args(["root", "--emit"]).served_at == "library"
    assert report.parse_args(["root", "--served-at", "personal", "--check"]).served_at == "personal"
    for argv in (["root"], ["root", "--emit", "--write"], ["root", "--served-at", "elsewhere", "--emit"]):
        with pytest.raises(SystemExit):
            report.parse_args(argv)


def test_emit_prints_one_object_and_writes_nothing(library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(library), "--emit") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n") and captured.out.count("\n") == 1 and captured.out.isascii()
    document = json.loads(captured.out)
    assert set(document) == {"json", "md"}
    assert _without_version(document["json"]) == GOLDEN[report.SCHEMA]["library"]["report"]
    assert document["md"].split("\n") == GOLDEN[report.SCHEMA]["library"]["md"]
    assert sorted(path.name for path in library.iterdir()) == ["screens"]


def test_emit_runs_isolated_on_a_read_only_tree(library: Path) -> None:
    """As the image check and the push review run it: ``python -I``, nothing written."""
    for path in [*library.rglob("*"), library]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "canopy_mcp.report", str(library), "--served-at", "library", "--emit"],
            capture_output=True,
            text=True,
            cwd=library,
            check=False,
        )
    finally:
        for path in [library, *library.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert done.returncode == 0, done.stderr
    assert _without_version(json.loads(done.stdout)["json"]) == GOLDEN[report.SCHEMA]["library"]["report"]


def test_write_then_check_passes(library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(library), "--write") == 0
    assert sorted(path.name for path in library.iterdir()) == ["LIBRARY-REPORT.json", "LIBRARY-REPORT.md", "screens"]
    stored = json.loads((library / "LIBRARY-REPORT.json").read_bytes())
    assert _without_version(stored) == GOLDEN[report.SCHEMA]["library"]["report"]
    assert "provenance" not in stored
    assert (library / "LIBRARY-REPORT.md").read_text().split("\n") == GOLDEN[report.SCHEMA]["library"]["md"]
    capsys.readouterr()
    assert _run(str(library), "--check") == 0
    captured = capsys.readouterr()
    assert captured.out == "report is current (8 files, --served-at library)\n" and captured.err == ""
    # The two report files are not part of the listing they describe.
    assert _run(str(library), "--write") == 0 and _run(str(library), "--check") == 0


def test_the_written_files_end_in_one_newline_and_have_no_trailing_whitespace(library: Path) -> None:
    command = "trailing space \nand a tab\t"
    button = {"id": "b", "type": "regular-button", "props": {"actions": [{"type": "execute", "command": command}]}}
    _put(library, "spaced", _screen(button, {"id": "t", "type": "text-label", "props": {"text": "café "}}))
    assert _run(str(library), "--write") == 0
    for name in ("LIBRARY-REPORT.json", "LIBRARY-REPORT.md"):
        data = (library / name).read_bytes()
        assert data.isascii() and b"\t" not in data and _clean_bytes(data), name


def test_write_takes_the_lock_through_lock_root(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[tuple[Path, bool]] = []

    def recording(path: Path, **kwargs: Any) -> Any:
        held.append((Path(path), not (library / "LIBRARY-REPORT.json").exists()))
        return lock_root(path, **kwargs)

    monkeypatch.setattr(report, "lock_root", recording)
    assert _run(str(library), "--write") == 0
    assert held == [(library, True)]
    held.clear()
    assert _run(str(library), "--emit") == 0 and _run(str(library), "--check") == 0
    assert held == []


def test_write_carries_an_existing_provenance_over_unchanged(library: Path) -> None:
    assert _run(str(library), "--write") == 0
    provenance = {"snapshot": {"path": "/s", "sha256": "ab", "time": "2026-01-01T00:00:00Z"}, "wheels": {"canopy": "1"}}
    stored = json.loads((library / "LIBRARY-REPORT.json").read_bytes())
    stored["provenance"] = provenance
    (library / "LIBRARY-REPORT.json").write_text(json.dumps(stored), encoding="utf-8")
    markdown = (library / "LIBRARY-REPORT.md").read_bytes()

    assert _run(str(library), "--check") == 0  # provenance is in no comparison
    _put(library, "new", _screen({"id": "s", "type": "pv-slider", "props": {"pv": "NEW"}}))
    assert _run(str(library), "--write") == 0
    rewritten = json.loads((library / "LIBRARY-REPORT.json").read_bytes())
    assert rewritten["provenance"] == provenance and list(rewritten)[-1] == "provenance"
    assert "NEW" in json.dumps(rewritten["writeBindings"])
    assert (library / "LIBRARY-REPORT.md").read_bytes() != markdown
    assert b"2026-01-01" not in (library / "LIBRARY-REPORT.md").read_bytes()
    assert _run(str(library), "--check") == 0


def test_a_report_that_differs_only_in_the_canopy_version_passes_check(library: Path) -> None:
    assert _run(str(library), "--write") == 0
    stored = json.loads((library / "LIBRARY-REPORT.json").read_bytes())
    stored["generator"]["canopy_version"] = "0.0.0+another"
    (library / "LIBRARY-REPORT.json").write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    assert _run(str(library), "--check") == 0
    stored["generator"]["schema"] = "canopy-library-report/0"
    (library / "LIBRARY-REPORT.json").write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    assert _run(str(library), "--check") == report.EXIT_MISMATCH


def test_check_fails_on_a_stale_report_and_prints_served_at(library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(library), "--write") == 0
    _put(library, "new", _screen({"id": "s", "type": "pv-slider", "props": {"pv": "NEW"}}))
    capsys.readouterr()
    assert _run(str(library), "--check") == report.EXIT_MISMATCH
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stale: LIBRARY-REPORT.json differs in: files, manifest, templates, screens, writeBindings\n" in captured.err
    assert "stale: LIBRARY-REPORT.md differs\n" in captured.err
    assert "recomputed with --served-at library" in captured.err
    assert "python -m canopy_mcp.report <root> --served-at library --write" in captured.err


def test_check_compares_the_markdown_byte_for_byte(library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(library), "--write") == 0
    markdown = library / "LIBRARY-REPORT.md"
    markdown.write_bytes(markdown.read_bytes() + b"\n")
    capsys.readouterr()
    assert _run(str(library), "--check") == report.EXIT_MISMATCH
    assert "stale: LIBRARY-REPORT.md differs\n" in capsys.readouterr().err
    markdown.unlink()
    assert _run(str(library), "--check") == report.EXIT_MISMATCH
    assert "LIBRARY-REPORT.md is missing or not readable" in capsys.readouterr().err


def test_check_with_another_served_at_names_the_value_it_used(tmp_path: Path, capsys) -> None:
    opens = {"type": "open_display", "file": "/abs.screen.json"}
    _put(tmp_path, "top", _screen({"id": "b", "type": "regular-button", "props": {"actions": [opens]}}))
    assert _run(str(tmp_path), "--served-at", "personal", "--write") == 0
    capsys.readouterr()
    assert _run(str(tmp_path), "--served-at", "personal", "--check") == 0
    assert _run(str(tmp_path), "--check") == report.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "stale: LIBRARY-REPORT.json differs in: rootRelativeRefs\n" in err
    assert "recomputed with --served-at library" in err
    assert (
        "refused: rootRelativeRefs: screen top node b at /root/children/0/props/actions/0/file: /abs.screen.json" in err
    )


def test_check_passes_on_a_missing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(tmp_path / "absent"), "--check") == 0
    assert capsys.readouterr().out == "no library\n"


@pytest.mark.parametrize("with_screens", [False, True])
def test_check_passes_on_an_empty_library_with_no_report(tmp_path: Path, capsys, with_screens: bool) -> None:
    if with_screens:
        (tmp_path / "screens" / "empty-dir").mkdir(parents=True)
    assert _run(str(tmp_path), "--check") == 0
    assert capsys.readouterr().out == "empty library, no report\n"


def test_check_fails_on_a_library_with_no_report(library: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(str(library), "--check") == report.EXIT_MISMATCH
    err = capsys.readouterr().err
    assert "stale: LIBRARY-REPORT.json is missing or not readable\n" in err
    assert "stale: LIBRARY-REPORT.md is missing or not readable\n" in err


def test_check_fails_on_an_empty_library_whose_report_lists_files(library: Path) -> None:
    assert _run(str(library), "--write") == 0
    shutil.rmtree(library / "screens")
    assert _run(str(library), "--check") == report.EXIT_MISMATCH
    assert _run(str(library), "--write") == 0 and _run(str(library), "--check") == 0


def test_check_refuses_the_refused_tree_even_with_a_current_report(tmp_path: Path, capsys) -> None:
    root = tmp_path / "library"
    shutil.copytree(TREES / "refused", root)
    assert _run(str(root), "--write") == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"canopy_mcp.report: warning: --check refuses this tree: {line}"
        for line in GOLDEN[report.SCHEMA]["refused"]["refusals"]
    ]
    assert _run(str(root), "--check") == report.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"canopy_mcp.report: refused: {line}" for line in GOLDEN[report.SCHEMA]["refused"]["refusals"]
    ]


def _refusing(root: Path, case: str) -> None:
    if case == "svg":
        (root / "screens" / "icon.SVG").write_text("<svg/>\n")
    elif case == "listing: other suffix":
        (root / "screens" / "notes.txt").write_text("x\n")
    elif case == "listing: outside screens":
        (root / "extra").mkdir()
    elif case == "listing: symlink":
        (root / "screens" / "link.png").symlink_to("top/logo.png")
    elif case == "listing: dotfile":
        (root / "screens" / ".hidden").write_text("x\n")
    elif case == "listing: leftover tmp":
        (root / ".LIBRARY-REPORT.json.abc.tmp").write_text("x\n")
    elif case == "listing: ignored name":
        (root / "screens" / "img.log").mkdir()
    elif case == "cross-root ref":
        embed = {"id": "e", "type": "embedded-display", "props": {"file": "../../screens/p.screen.json"}}
        _put(root, "x", _screen(embed))
    elif case == "root-relative ref":
        opens = {"type": "open_display", "file": "library/x.screen.json"}
        _put(root, "x", _screen({"id": "b", "type": "regular-button", "props": {"actions": [opens]}}))
    elif case == "rule retargets a channel":
        rules = [{"targetProperty": "pvName", "pvs": [], "expressions": []}]
        _put(root, "x", _screen({"id": "l", "type": "pv-label", "props": {}, "rules": rules}))
    elif case == "rule retargets a ref":
        rules = [{"targetProperty": "src", "pvs": [], "expressions": []}]
        _put(root, "x", _screen({"id": "i", "type": "image-view", "props": {}, "rules": rules}))
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "svg",
        "listing: other suffix",
        "listing: outside screens",
        "listing: symlink",
        "listing: dotfile",
        "listing: leftover tmp",
        "listing: ignored name",
        "cross-root ref",
        "root-relative ref",
        "rule retargets a channel",
        "rule retargets a ref",
    ],
)
def test_check_refuses(library: Path, capsys: pytest.CaptureFixture[str], case: str) -> None:
    _refusing(library, case)
    assert _run(str(library), "--write") == 0  # the report itself is current
    capsys.readouterr()
    assert _run(str(library), "--check") == report.EXIT_REFUSED
    err = capsys.readouterr().err
    assert err.startswith("canopy_mcp.report: refused: ") and "stale" not in err


def test_a_ds_store_does_not_fail_the_check(library: Path) -> None:
    assert _run(str(library), "--write") == 0
    (library / ".DS_Store").write_bytes(b"\x00")
    (library / "screens" / "top" / ".DS_Store").write_bytes(b"\x00")
    assert _run(str(library), "--check") == 0


def test_write_on_a_missing_root_fails_without_creating_it(tmp_path: Path, capsys) -> None:
    assert _run(str(tmp_path / "absent"), "--write") == report.EXIT_MISMATCH
    assert not (tmp_path / "absent").exists()
    assert capsys.readouterr().err.startswith("canopy_mcp.report: cannot read or write ")


def test_a_screen_that_changes_under_the_report_refuses(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.read_bytes
    target = library / "screens" / "top" / "other.screen.json"
    calls = {"n": 0}

    def racing(self: Path) -> bytes:
        data = real(self)
        if self == target:
            calls["n"] += 1
            if calls["n"] == 2:  # the scan hashed it; the report now reads other bytes
                return data + b" "
        return data

    monkeypatch.setattr(Path, "read_bytes", racing)
    with pytest.raises(report.ReportRefused, match="^changed_while_read: screens/top/other.screen.json"):
        report.report_for_tree(library, served_at="library")


def test_not_mcp_tools() -> None:
    for module in (report, listing):
        source = Path(module.__file__).read_text()
        assert "fastmcp" not in source and "mcp.tool" not in source
