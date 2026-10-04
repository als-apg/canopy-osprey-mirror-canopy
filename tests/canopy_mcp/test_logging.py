"""canopy_mcp entry-point logging: records keep their level and logger name, on stderr."""

import os
import sys
import subprocess
from pathlib import Path

import pytest

from canopy_mcp import __main__ as entry

FIXTURES = Path(__file__).resolve().parents[1] / "_fixtures" / "schemes"

# Configure logging the way `python -m canopy_mcp --http` does, then load a
# broken scheme through the real loader, which reports it with logger.error.
_CHILD = """
import sys
from canopy_mcp.__main__ import configure_logging
from canopy.core.color_scheme import get_color_scheme

configure_logging()
get_color_scheme(sys.argv[1])
"""


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CANOPY_")}
    env["CANOPY_LOG_FORMAT"] = "text"
    return env


def test_color_scheme_error_renders_level_and_logger_on_stderr(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(FIXTURES / "unparseable.json")],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=tmp_path,  # no stray .env picked up by the settings loader
        timeout=60,
        check=True,
    )

    assert proc.stdout == ""
    lines = [line for line in proc.stderr.splitlines() if "canopy.core.color_scheme" in line]
    assert len(lines) == 1, proc.stderr
    assert "[ERROR] canopy.core.color_scheme: color scheme " in lines[0]
    assert "unparseable.json" in lines[0]


# pytest attaches its own capture handlers to the root logger during a test,
# so the root-handler checks below run in a fresh interpreter.
_HANDLERS_CHILD = """
import sys
import logging
from canopy_mcp.__main__ import configure_logging

root = logging.getLogger()
if sys.argv[1] == "host":
    host = logging.NullHandler()
    root.addHandler(host)
    root.setLevel(logging.WARNING)
configure_logging()
configure_logging()
print(len(root.handlers), logging.getLevelName(root.level))
for handler in root.handlers:
    stream = getattr(handler, "stream", None)
    print(type(handler).__name__, "stderr" if stream is sys.stderr else "stdout" if stream is sys.stdout else "-")
"""


def _handlers(mode: str, cwd: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _HANDLERS_CHILD, mode],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=cwd,
        timeout=60,
        check=True,
    )
    return proc.stdout.splitlines()


def test_configure_logging_attaches_one_stderr_handler_once(tmp_path):
    assert _handlers("bare", tmp_path) == ["1 INFO", "StreamHandler stderr"]


def test_configure_logging_leaves_a_configured_root_alone(tmp_path):
    assert _handlers("host", tmp_path) == ["1 WARNING", "NullHandler -"]


def test_main_configures_logging_before_serving(monkeypatch):
    import canopy_mcp.app as app_mod

    order = []
    monkeypatch.setattr(entry, "configure_logging", lambda: order.append("logging"))
    monkeypatch.setattr(app_mod, "create_app", lambda **_: order.append("app"))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: order.append("run"))

    assert entry.main(["--http"]) == 0
    assert order == ["logging", "app", "run"]


def test_stdio_refusal_does_not_configure_logging(monkeypatch):
    monkeypatch.setattr(entry, "configure_logging", lambda: pytest.fail("configured logging"))
    assert entry.main([]) == 2
