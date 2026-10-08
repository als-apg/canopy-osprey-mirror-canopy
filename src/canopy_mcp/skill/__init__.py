"""The ``canopy-panel-builder`` skill: the agent's guide to the panel-builder tools.

``SKILL.md`` in this package is the single source of the text. It ships in the
wheel as package data, and ``python -m canopy_mcp.skill install <dir>`` writes
it to ``<dir>/.claude/skills/canopy-panel-builder/SKILL.md``. A deployment that
keeps a copy of its own compares it with :func:`skill_bytes`, so the text is
only ever handled as bytes: no newline translation, no template.

Importing this package needs nothing but the standard library, so the install
command runs wherever the wheel is installed, with or without the ``mcp`` extra.
"""

import os
import tempfile
from pathlib import Path
from importlib.resources import files

SKILL_NAME = "canopy-panel-builder"
SKILL_FILE = "SKILL.md"

__all__ = ["SKILL_FILE", "SKILL_NAME", "install", "skill_bytes", "skill_path"]


def skill_bytes() -> bytes:
    """The skill text exactly as shipped."""
    return (files(__name__) / SKILL_FILE).read_bytes()


def skill_path(directory: Path) -> Path:
    """Where the skill of project ``directory`` lives."""
    return directory / ".claude" / "skills" / SKILL_NAME / SKILL_FILE


def install(directory: Path) -> Path:
    """Write the skill into project ``directory`` and return the file's path.

    Missing directories are created. A copy that already holds the same bytes
    is left alone; any other is replaced atomically (a temporary file beside
    it, then ``os.replace``), so a reader never sees half a skill.
    """
    data = skill_bytes()
    target = skill_path(directory)
    try:
        if target.read_bytes() == data:
            return target
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{SKILL_FILE}.", suffix=".tmp")
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
