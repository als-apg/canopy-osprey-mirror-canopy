"""Entry point: ``python -m canopy_mcp.skill install <dir>``.

Writes the ``canopy-panel-builder`` skill to
``<dir>/.claude/skills/canopy-panel-builder/SKILL.md``, the place an agent
working in project ``<dir>`` looks for it, and prints that path. The bytes are
those of the packaged file. Running it again is safe: an up-to-date copy is left
alone and an older one is replaced.
"""

import sys
import argparse
from pathlib import Path
from collections.abc import Sequence

from canopy_mcp.skill import SKILL_NAME, install, skill_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m canopy_mcp.skill",
        description=f"Install the {SKILL_NAME} skill into a project.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="install")
    install_parser = commands.add_parser("install", help="write the skill into a project directory")
    install_parser.add_argument(
        "directory",
        type=Path,
        metavar="dir",
        help=f"the project directory; the skill is written to {skill_path(Path('<dir>'))}",
    )
    return parser.parse_args(argv)


def _fail(message: str) -> int:
    print(f"canopy_mcp.skill: {message}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        target = install(args.directory)
    except OSError as exc:
        return _fail(f"cannot write the skill into {args.directory}: {exc.strerror or type(exc).__name__}")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
