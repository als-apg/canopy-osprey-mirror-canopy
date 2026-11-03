"""The Canopy skills: one source file each, shipped with the package and installed byte for byte.

``canopy_mcp.skill.SKILLS`` names them and their sources under
``src/canopy_mcp/skill/``. Deployments hold copies that are compared with them
by bytes, so the files are read and written as bytes everywhere, and their
whitespace is asserted here: the repository's whitespace hooks cover ``.py`` and
``.txt`` only. The names and source paths are a contract: hosts iterate
``SKILLS`` to check their copies.
"""

from __future__ import annotations

import re
import sys
import json
import tomllib
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from importlib.resources import files

import pytest

from canopy_mcp import skill
from canopy_mcp.skill import __main__ as cli
from canopy_mcp.server import AUTHORING_TOOLS, mcp, register_all
from canopy_mcp.check.schema import check_schema
from canopy_mcp.check.semantic import iter_nodes, check_semantic
from tests.canopy_mcp.synthetic_spec import write_spec

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "canopy_mcp" / "skill"
SOURCE = PACKAGE / "SKILL.md"
INSTALLED = Path(".claude") / "skills" / "canopy-panel-builder" / "SKILL.md"

# Every skill: its source file in the repository and where it lands in a project.
SOURCES = {"canopy-panel-builder": SOURCE}
INSTALLED_AS = {name: Path(".claude") / "skills" / name / "SKILL.md" for name in SOURCES}
NAMES = list(SOURCES)

SOURCE_BYTES = SOURCE.read_bytes()
TEXT = SOURCE_BYTES.decode("utf-8")


async def _tool_names() -> set[str]:
    register_all()
    return {tool.name for tool in await mcp.list_tools()}


# --- one source, the same bytes everywhere -------------------------------------------------


def test_the_registry_names_every_skill_and_its_source() -> None:
    assert skill.SKILLS == {"canopy-panel-builder": "SKILL.md"}
    for name, relative in skill.SKILLS.items():
        assert PACKAGE / relative == SOURCES[name]


def test_the_builder_keeps_its_names_for_other_hosts() -> None:
    assert skill.SKILL_NAME == "canopy-panel-builder"
    assert skill.SKILL_FILE == "SKILL.md"
    assert skill.SKILLS[skill.SKILL_NAME] == skill.SKILL_FILE
    assert skill.skill_bytes() == skill.skill_bytes(skill.SKILL_NAME) == SOURCE_BYTES
    assert skill.skill_path(Path("project")) == Path("project") / INSTALLED


@pytest.mark.parametrize("name", NAMES)
def test_the_package_serves_the_bytes_of_the_source_file(name: str) -> None:
    source = SOURCES[name].read_bytes()
    assert files("canopy_mcp.skill").joinpath(*skill.SKILLS[name].split("/")).read_bytes() == source
    assert skill.skill_bytes(name) == source


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_is_named_after_the_directory_it_installs_into(name: str) -> None:
    assert skill.skill_path(Path("project"), name) == Path("project") / INSTALLED_AS[name]


def test_an_unknown_skill_name_is_refused() -> None:
    with pytest.raises(KeyError, match="canopy-panel-builder"):
        skill.skill_bytes("canopy-nothing")
    with pytest.raises(KeyError, match="canopy-panel-builder"):
        skill.skill_path(Path("project"), "canopy-nothing")


def test_install_returns_every_installed_path_in_registry_order(tmp_path: Path) -> None:
    assert skill.install(tmp_path) == [tmp_path / INSTALLED_AS[name] for name in skill.SKILLS]


def test_install_writes_the_same_bytes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", str(tmp_path)]) == 0

    targets = [tmp_path / INSTALLED_AS[name] for name in NAMES]
    assert capsys.readouterr().out.splitlines() == [str(target) for target in targets]
    for name, target in zip(NAMES, targets, strict=True):
        assert target.read_bytes() == SOURCES[name].read_bytes()
        # Nothing but the skill: no temporary file is left beside it.
        assert [path.name for path in target.parent.iterdir()] == ["SKILL.md"]
    assert sorted(path.name for path in (tmp_path / ".claude" / "skills").iterdir()) == sorted(NAMES)


def test_install_is_idempotent(tmp_path: Path) -> None:
    assert cli.main(["install", str(tmp_path)]) == 0
    targets = {name: tmp_path / INSTALLED_AS[name] for name in NAMES}
    first = {name: target.stat() for name, target in targets.items()}

    assert cli.main(["install", str(tmp_path)]) == 0

    for name, target in targets.items():
        assert target.read_bytes() == SOURCES[name].read_bytes()
        # An up-to-date copy is left alone, not rewritten.
        assert (target.stat().st_ino, target.stat().st_mtime_ns) == (first[name].st_ino, first[name].st_mtime_ns)
        assert [path.name for path in target.parent.iterdir()] == ["SKILL.md"]


@pytest.mark.parametrize("name", NAMES)
def test_install_replaces_a_copy_that_differs(tmp_path: Path, name: str) -> None:
    target = tmp_path / INSTALLED_AS[name]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"an older skill\n")

    assert cli.main(["install", str(tmp_path)]) == 0

    assert target.read_bytes() == SOURCES[name].read_bytes()
    assert [path.name for path in target.parent.iterdir()] == ["SKILL.md"]


def test_install_keeps_what_else_the_project_holds(tmp_path: Path) -> None:
    other = tmp_path / ".claude" / "skills" / "another" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"mine\n")

    assert cli.main(["install", str(tmp_path)]) == 0

    assert other.read_bytes() == b"mine\n"
    for name in NAMES:
        assert (tmp_path / INSTALLED_AS[name]).read_bytes() == SOURCES[name].read_bytes()


def test_install_creates_a_project_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    project = tmp_path / "new" / "project"

    assert cli.main(["install", str(project)]) == 0

    for name in NAMES:
        assert (project / INSTALLED_AS[name]).read_bytes() == SOURCES[name].read_bytes()


def test_install_refuses_a_target_that_is_not_a_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    occupied = tmp_path / "project"
    occupied.write_bytes(b"a file\n")

    assert cli.main(["install", str(occupied)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("canopy_mcp.skill: ")
    assert str(occupied) in captured.err
    assert occupied.read_bytes() == b"a file\n"


@pytest.mark.parametrize("argv", [[], ["install"], ["remove", "x"]])
def test_a_command_line_without_install_and_a_directory_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2


def test_install_runs_isolated_and_needs_no_server_dependency(tmp_path: Path) -> None:
    """``python -I -m canopy_mcp.skill install``: the documented command, with nothing but the package."""
    done = subprocess.run(
        [sys.executable, "-I", "-m", "canopy_mcp.skill", "install", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    for name in NAMES:
        assert (tmp_path / INSTALLED_AS[name]).read_bytes() == SOURCES[name].read_bytes()

    heavy = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, canopy_mcp.skill.__main__\n"
            "print(sorted(m for m in ('fastmcp', 'playwright', 'canopy_mcp.server', 'uvicorn') if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert heavy.returncode == 0, heavy.stderr
    assert heavy.stdout.strip() == "[]"


# --- the file itself ----------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_has_no_trailing_whitespace_and_ends_in_exactly_one_newline(name: str) -> None:
    data = SOURCES[name].read_bytes()
    assert b"\r" not in data
    assert b"\t" not in data
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")
    assert not data.startswith((b"\n", b"\xef\xbb\xbf"))
    text = data.decode("utf-8")
    trailing = [number for number, line in enumerate(text.split("\n"), start=1) if line != line.rstrip()]
    assert trailing == []


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_is_ascii(name: str) -> None:
    """Copies are compared by bytes, so no character may have two encodings or an invisible twin."""
    text = SOURCES[name].read_bytes().decode("utf-8")
    assert [char for char in text if ord(char) > 126 or (ord(char) < 32 and char != "\n")] == []


@pytest.mark.parametrize("name", NAMES)
def test_the_frontmatter_names_the_skill(name: str) -> None:
    text = SOURCES[name].read_bytes().decode("utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match is not None
    fields = dict(line.split(": ", 1) for line in match.group(1).split("\n"))
    assert set(fields) == {"name", "description"}
    assert fields["name"] == name
    assert 40 <= len(fields["description"]) <= 1024


async def test_the_skill_names_every_tool_the_server_registers() -> None:
    names = await _tool_names()
    assert names
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", TEXT, flags=re.MULTILINE))
    assert documented == names


async def test_the_skill_marks_exactly_the_authoring_tools_as_writing() -> None:
    rows = dict(re.findall(r"^\| `([a-z_]+)` \| (reads|writes) \|", TEXT, flags=re.MULTILINE))
    assert set(rows) == await _tool_names()
    assert {name for name, kind in rows.items() if kind == "writes"} == set(AUTHORING_TOOLS)
    # The text states no tool counts, which drift whenever a tool is added.
    assert re.search(r"\b\d+ tools\b", TEXT) is None


@pytest.mark.parametrize(
    "phrase",
    [
        "`CANOPY_DRAFTS_DIR`",
        "your screens root",
        "`<your screens root>/screens/rf/status.screen.json`",
        "`CANOPY_PV_MODE=mock`",
        "`base_rev`",
        "`stale_rev`",
        "`library/<id>`",
        "`schemaVersion`",
        "`waveform-plot`",
        "`xy-plot`",
        "`pv-array-plot`",
        "`y_channel`",
        "`y_channels`",
        "Trace tab",
        "`list_collections`",
        "`open_url`",
    ],
)
def test_the_skill_covers(phrase: str) -> None:
    assert phrase in TEXT


def test_the_example_screen_passes_the_schema_and_semantic_checks(tmp_path: Path) -> None:
    """The one document an agent copies from must be one the server would take."""
    blocks = re.findall(r"```json\n(.*?)\n```", TEXT, flags=re.DOTALL)
    assert len(blocks) == 1
    document = json.loads(blocks[0])
    assert check_schema(document, write_spec(tmp_path / "spec")) == []
    assert check_semantic(document) == []
    known = set(json.loads((Path(__file__).parent / "fixtures" / "ir-map.json").read_text()))
    assert {node["type"] for _, node in iter_nodes(document)} <= known


def test_the_skill_sends_a_scalar_time_series_to_trace_not_to_a_widget() -> None:
    """The plots section names the three charts and says a time series of a scalar is the Trace tab's job."""
    section = TEXT.split("\n## Plots\n", 1)[1].split("\n## ", 1)[0]
    for widget in ("`waveform-plot`", "`xy-plot`", "`pv-array-plot`"):
        assert widget in section
    assert "`y_channel`" in section and "`y_channels`" in section
    assert "not a widget" in section and "Trace tab" in section and "correlation page" in section
    assert "no Trace tab" in section


def test_the_skill_sends_a_live_array_view_to_the_waveform_tab() -> None:
    """A live view of array PVs, overlays or the delta to a reference is the Waveform tab's job."""
    section = TEXT.split("\n## Plots\n", 1)[1].split("\n## ", 1)[0]
    paragraph = next(block for block in section.split("\n\n") if "Waveform tab" in block)
    for phrase in ("array PV", "overlay", "delta", "reference", "beside other controls", "`waveform-plot`"):
        assert phrase in paragraph
    assert "`waveform_tab`" in paragraph
    assert "no Waveform tab" in paragraph


def test_the_skill_keeps_edits_of_an_existing_draft_on_patch_node() -> None:
    paragraph = next(block for block in TEXT.split("\n\n") if "`overwrite=true` it replaces" in block)
    assert "`patch_node`" in paragraph and "`add_node`" in paragraph and "`remove_node`" in paragraph
    assert "`new_screen` is for a new draft" in paragraph
    assert "Read the draft once" in paragraph


def test_the_skill_is_generic() -> None:
    """One text for every deployment: it names no integration, host or absolute path."""
    lowered = TEXT.lower()
    assert "osprey" not in lowered
    assert "/users/" not in lowered and "/home/" not in lowered and "/app/" not in lowered


# --- the pointer on the server ------------------------------------------------------------


def test_the_server_instructions_point_at_the_skills_in_one_line() -> None:
    assert isinstance(mcp.instructions, str)
    assert "\n" not in mcp.instructions
    for name in skill.SKILLS:
        assert name in mcp.instructions
    assert "python -m canopy_mcp.skill install" in mcp.instructions


# --- the wheel ----------------------------------------------------------------------------


def test_the_wheel_target_ships_the_package_directory_with_its_markdown() -> None:
    """The real-wheel check is the payload build's; this pins the configuration it relies on."""
    hatch = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]
    wheel = hatch["targets"]["wheel"]
    assert "src/canopy_mcp" in wheel["packages"]

    shipped = [
        *(str(path.relative_to(REPO)) for path in SOURCES.values()),
        "canopy_mcp/skill/SKILL.md",
        "canopy_mcp/skill/operator/SKILL.md",
        "skill/SKILL.md",
        "operator/SKILL.md",
        "SKILL.md",
        "x.md",
    ]
    excludes = [*hatch.get("exclude", []), *wheel.get("exclude", [])]
    hits = [
        pattern
        for pattern in excludes
        if any(fnmatch(path, pattern.lstrip("/")) or fnmatch(path, f"*{pattern}") for path in shipped)
    ]
    assert hits == []
    # A narrowing table would drop the file as surely as an exclude.
    assert "only-include" not in wheel and "include" not in wheel
    assert "only-include" not in hatch and "include" not in hatch


@pytest.mark.parametrize("name", NAMES)
def test_the_source_file_is_not_ignored_by_git(name: str) -> None:
    """hatchling leaves out what git ignores, so an ignore rule would empty the wheel of the skill."""
    done = subprocess.run(
        ["git", "check-ignore", "-q", str(SOURCES[name].relative_to(REPO))],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
