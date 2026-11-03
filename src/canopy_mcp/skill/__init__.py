"""The Canopy skills: the agent's guides to the canopy MCP tools.

:data:`SKILLS` maps each skill's name to its source file in this package, which
is the single source of the text: ``canopy-panel-builder`` (building screens)
and ``canopy-operator`` (operating the workspace). The files ship in the wheel
as package data, and ``python -m canopy_mcp.skill install <dir>`` writes them
in the Claude Code skill layout, each to ``<dir>/.claude/skills/<name>/SKILL.md``.
The text is only ever handled as bytes (:func:`skill_bytes`): no newline
translation, no template.

:data:`SKILL_NAME` and :data:`SKILL_FILE` name the panel-builder entry, the
default of every function that takes a skill name.

Importing this package needs nothing but the standard library, so the install
command runs wherever the wheel is installed, with or without the ``mcp`` extra.
"""

import os
import tempfile
from pathlib import Path
from importlib.resources import files

SKILLS: dict[str, str] = {
    "canopy-panel-builder": "SKILL.md",
    "canopy-operator": "operator/SKILL.md",
}

SKILL_NAME = "canopy-panel-builder"
SKILL_FILE = SKILLS[SKILL_NAME]

# The file name every skill has once installed, whatever its source path.
_INSTALLED_FILE = "SKILL.md"

__all__ = ["SKILLS", "SKILL_FILE", "SKILL_NAME", "install", "skill_bytes", "skill_path"]


def _source(name: str) -> str:
    try:
        return SKILLS[name]
    except KeyError:
        raise KeyError(f"no skill named {name!r}; the skills are {', '.join(SKILLS)}") from None


def skill_bytes(name: str = SKILL_NAME) -> bytes:
    """The text of skill ``name`` exactly as shipped."""
    return files(__name__).joinpath(*_source(name).split("/")).read_bytes()


def skill_path(directory: Path, name: str = SKILL_NAME) -> Path:
    """Where skill ``name`` of project ``directory`` lives."""
    _source(name)
    return directory / ".claude" / "skills" / name / _INSTALLED_FILE


def _install_one(directory: Path, name: str) -> Path:
    data = skill_bytes(name)
    target = skill_path(directory, name)
    try:
        if target.read_bytes() == data:
            return target
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{_INSTALLED_FILE}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp makes the file private; a skill is read by whoever uses the project.
        umask = os.umask(0)
        os.umask(umask)
        tmp.chmod(0o666 & ~umask)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def install(directory: Path) -> list[Path]:
    """Write every skill into project ``directory`` and return the files' paths, in :data:`SKILLS` order.

    Missing directories are created. A copy that already holds the same bytes
    is left alone; any other is replaced atomically (a temporary file beside
    it, then ``os.replace``), so a reader never sees half a skill.
    """
    return [_install_one(directory, name) for name in SKILLS]
