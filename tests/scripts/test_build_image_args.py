"""Argument handling of scripts/build-image.sh and scripts/verify-image.sh.

Everything runs through ``--dry-run`` (or verify's usage check) against
throwaway git repositories under ``tmp_path``: the scripts are copied into a
temp canopy repo together with the real Dockerfile.appliance, and docker, pnpm,
uv and uvx are replaced by stubs that record any call and fail, so a dry run
that tried to build would be caught.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "scripts" / "build-image.sh"
VERIFY = REPO / "scripts" / "verify-image.sh"
DOCKERFILE = REPO / "docker" / "Dockerfile.appliance"

SIX_ARGS = {
    "CANOPY_SHA",
    "RUNTIME_SHA",
    "TRACE_SHA",
    "TRACE_LOCK_SHA256",
    "CANOPY_VERSION",
    "PYDM_VERSION",
}
LOCK_TEXT = "lockfileVersion: '9.0'\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path) -> dict:
    can = tmp_path / "can"
    (can / "scripts").mkdir(parents=True)
    (can / "docker").mkdir()
    shutil.copy2(BUILD, can / "scripts" / "build-image.sh")
    shutil.copy2(VERIFY, can / "scripts" / "verify-image.sh")
    shutil.copy2(DOCKERFILE, can / "docker" / "Dockerfile.appliance")
    git(can, "init", "-q", "-b", "main")
    git(can, "add", "-A")
    git(can, "commit", "-q", "-m", "init")

    pydm = make_repo(tmp_path / "pydm", {"pyproject.toml": "[project]\nname='PyDMConverter'\n"})
    rt = make_repo(tmp_path / "rt", {"package.json": "{}\n", "apps/viewer/package.json": "{}\n"})
    trace = make_repo(tmp_path / "trace", {"package.json": "{}\n", "pnpm-lock.yaml": LOCK_TEXT})
    git(trace, "branch", "feature")
    # A second commit on main that drops the lockfile: main has none, feature has one.
    git(trace, "rm", "-q", "pnpm-lock.yaml")
    git(trace, "commit", "-q", "-m", "drop lock")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "tool-called"
    for tool in ("docker", "pnpm", "uv", "uvx"):
        stub = bin_dir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{marker}"\nexit 97\n')
        stub.chmod(0o755)

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYDM", "RT", "TRACE", "TRACE_REF", "TAG", "PLATFORM", "CANOPY_VERSION", "PYDM_VERSION"}
    }
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TMPDIR"] = str(tmp_path)
    env.update(
        PYDM=str(pydm),
        RT=str(rt),
        TRACE=str(trace),
        TRACE_REF="feature",
        CANOPY_VERSION="1.2.3",
        PYDM_VERSION="4.5.6",
    )
    return {"can": can, "pydm": pydm, "rt": rt, "trace": trace, "env": env, "marker": marker}


def run(world: dict, script: str, *args: str, unset: tuple[str, ...] = (), **extra: str):
    env = {k: v for k, v in world["env"].items() if k not in unset}
    env.update(extra)
    return subprocess.run(
        ["bash", str(world["can"] / "scripts" / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def dry(world: dict, *args: str, **kw):
    return run(world, "build-image.sh", "--dry-run", *args, **kw)


def ok_argv(proc) -> list[str]:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return shlex.split(proc.stdout.strip().splitlines()[-1])


def pairs(argv: list[str], flag: str) -> dict[str, str]:
    out = {}
    for i, a in enumerate(argv[:-1]):
        if a == flag:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


def assert_no_tool_called(world: dict) -> None:
    assert not world["marker"].exists(), world["marker"].read_text()


# --- build-image.sh: refusals ---------------------------------------------------


def test_missing_trace_without_no_trace_fails(world):
    proc = dry(world, unset=("TRACE",))
    assert proc.returncode != 0
    assert "TRACE" in proc.stderr
    assert_no_tool_called(world)


def test_missing_trace_ref_without_no_trace_fails(world):
    proc = dry(world, unset=("TRACE_REF",))
    assert proc.returncode != 0
    assert "TRACE_REF" in proc.stderr


@pytest.mark.parametrize("var", ["PYDM", "RT"])
def test_missing_required_checkout_fails(world, var):
    proc = dry(world, "--no-trace", unset=(var,))
    assert proc.returncode != 0
    assert var in proc.stderr


def test_unresolvable_ref_fails(world):
    proc = dry(world, TRACE_REF="no-such-branch")
    assert proc.returncode != 0
    assert "no-such-branch" in proc.stderr
    assert_no_tool_called(world)


def test_ref_without_lockfile_fails(world):
    proc = dry(world, TRACE_REF="main")
    assert proc.returncode != 0
    assert "pnpm-lock.yaml" in proc.stderr


def test_pydm_with_tracked_changes_is_refused(world):
    (world["pydm"] / "pyproject.toml").write_text("changed\n")
    proc = dry(world)
    assert proc.returncode != 0
    assert "PYDM" in proc.stderr and "tracked" in proc.stderr


def test_unknown_argument_is_a_usage_error(world):
    proc = run(world, "build-image.sh", "--bogus")
    assert proc.returncode == 2


def test_help_lists_inputs(world):
    proc = run(world, "build-image.sh", "--help", unset=("PYDM", "RT", "TRACE", "TRACE_REF"))
    assert proc.returncode == 0
    for name in ("PYDM", "RT", "TRACE", "TRACE_REF", "TAG", "PLATFORM", "--no-trace", "--dry-run"):
        assert name in proc.stdout


# --- build-image.sh: the argv ------------------------------------------------------


def test_argv_carries_all_build_args_and_contexts(world):
    proc = dry(world)
    argv = ok_argv(proc)
    assert argv[:2] == ["docker", "build"]
    args = pairs(argv, "--build-arg")
    assert set(args) == SIX_ARGS
    contexts = pairs(argv, "--build-context")
    assert {"canopy", "pydm-src", "runtime", "viewer", "pydm", "trace-src"} <= set(contexts)
    assert "trace" not in contexts  # the trace stage is built from trace-src
    assert contexts["canopy"] == str(world["can"].resolve())
    assert contexts["runtime"] == str(world["rt"])
    assert contexts["viewer"] == f"{world['rt']}/apps/viewer/dist"
    assert contexts["pydm-src"] != str(world["pydm"])  # an archived copy, not the checkout
    assert argv[argv.index("-f") + 1].endswith("docker/Dockerfile.appliance")
    assert_no_tool_called(world)


def test_resolved_shas_and_lock_hash(world):
    proc = dry(world)
    args = pairs(ok_argv(proc), "--build-arg")
    trace_sha = git(world["trace"], "rev-parse", "feature")
    assert args["TRACE_SHA"] == trace_sha
    assert args["TRACE_LOCK_SHA256"] == hashlib.sha256(LOCK_TEXT.encode()).hexdigest()
    assert args["CANOPY_SHA"] == head(world["can"])
    assert args["RUNTIME_SHA"] == head(world["rt"])
    assert args["CANOPY_VERSION"] == "1.2.3"
    assert args["PYDM_VERSION"] == "4.5.6"
    assert trace_sha in proc.stderr  # resolved SHA is printed


def test_dirty_only_for_tracked_changes(world):
    (world["rt"] / "untracked.txt").write_text("x\n")
    (world["can"] / "untracked.txt").write_text("x\n")
    args = pairs(ok_argv(dry(world)), "--build-arg")
    assert args["RUNTIME_SHA"] == head(world["rt"])
    assert args["CANOPY_SHA"] == head(world["can"])

    (world["rt"] / "package.json").write_text('{"x": 1}\n')
    (world["can"] / "docker" / "Dockerfile.appliance").write_text(DOCKERFILE.read_text() + "\n")
    args = pairs(ok_argv(dry(world)), "--build-arg")
    assert args["RUNTIME_SHA"] == head(world["rt"]) + "+dirty"
    assert args["CANOPY_SHA"] == head(world["can"]) + "+dirty"


def test_no_trace_passes_with_empty_trace_contexts(world):
    proc = dry(world, "--no-trace", unset=("TRACE", "TRACE_REF"))
    argv = ok_argv(proc)
    contexts = pairs(argv, "--build-context")
    assert {"trace-src", "trace"} <= set(contexts)
    args = pairs(argv, "--build-arg")
    assert set(args) == SIX_ARGS
    assert args["TRACE_SHA"] == "" and args["TRACE_LOCK_SHA256"] == ""
    assert_no_tool_called(world)


def test_platform_only_when_set(world):
    assert "--platform" not in ok_argv(dry(world))
    argv = ok_argv(dry(world, PLATFORM="linux/amd64"))
    assert argv[argv.index("--platform") + 1] == "linux/amd64"


def test_tag_default_and_override(world):
    argv = ok_argv(dry(world))
    assert argv[argv.index("-t") + 1] == "canopy:dev"
    argv = ok_argv(dry(world, TAG="canopy:test-1"))
    assert argv[argv.index("-t") + 1] == "canopy:test-1"


def test_build_args_are_declared_in_the_dockerfile(world):
    declared = set(re.findall(r"^ARG (\w+)", DOCKERFILE.read_text(), re.M))
    for extra in ((), ("--no-trace",)):
        args = pairs(ok_argv(dry(world, *extra)), "--build-arg")
        assert set(args) <= declared


def test_no_uv_sources_pin():
    text = BUILD.read_text()
    assert "tool.uv.sources" not in text and "uv add" not in text


# --- verify-image.sh: usage ------------------------------------------------------


@pytest.mark.parametrize("var", ["TRACE_REF", "TRACE", "RT"])
def test_verify_without_input_is_usage_error(world, var):
    proc = run(world, "verify-image.sh", "canopy:dev", unset=(var,))
    assert proc.returncode == 2
    assert var in proc.stderr and "Usage" in proc.stderr
    assert_no_tool_called(world)


def test_verify_without_image_is_usage_error(world):
    proc = run(world, "verify-image.sh")
    assert proc.returncode == 2
    assert_no_tool_called(world)


# --- build-image.sh --worktree ------------------------------------------------------

FP = re.compile(r"\+dirty\.[0-9a-f]{16}")


def test_worktree_refuses_a_ref_that_is_not_trace_head(world):
    proc = dry(world, "--worktree")  # TRACE_REF=feature, TRACE HEAD is main
    assert proc.returncode != 0
    assert "is not the HEAD of TRACE" in proc.stderr
    assert_no_tool_called(world)


def test_worktree_labels_pin_working_tree_content(world):
    git(world["trace"], "checkout", "-q", "feature")
    clean = pairs(ok_argv(dry(world, "--worktree")), "--build-arg")
    assert clean["CANOPY_SHA"] == head(world["can"])
    assert clean["TRACE_SHA"] == head(world["trace"])

    # An untracked file counts as dirty here (it is part of the built context).
    (world["can"] / "untracked.txt").write_text("x\n")
    (world["trace"] / "new.ts").write_text("export {}\n")
    args = pairs(ok_argv(dry(world, "--worktree")), "--build-arg")
    assert FP.fullmatch(args["CANOPY_SHA"].removeprefix(head(world["can"])))
    assert FP.fullmatch(args["TRACE_SHA"].removeprefix(head(world["trace"])))
    assert args["RUNTIME_SHA"] == head(world["rt"])
    assert args["TRACE_LOCK_SHA256"] == hashlib.sha256(LOCK_TEXT.encode()).hexdigest()

    # Same content, same label; changed content, different fingerprint.
    assert pairs(ok_argv(dry(world, "--worktree")), "--build-arg") == args
    (world["trace"] / "new.ts").write_text("export const x = 1\n")
    changed = pairs(ok_argv(dry(world, "--worktree")), "--build-arg")
    assert changed["TRACE_SHA"] != args["TRACE_SHA"]
    assert changed["CANOPY_SHA"] == args["CANOPY_SHA"]
    assert_no_tool_called(world)
