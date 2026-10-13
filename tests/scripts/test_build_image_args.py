"""Argument handling of scripts/build-image.sh and scripts/verify-image.sh.

Everything runs through ``--dry-run`` (or verify's usage check) against
throwaway git repositories under ``tmp_path``: the scripts are copied into a
temp canopy repo together with the real Dockerfile.appliance, and docker, pnpm,
uv and uvx are replaced by stubs that record any call and fail, so a dry run
that tried to build would be caught. The runtime repo links the SDK repo the way
a real one does (``apps/viewer/node_modules/@slaclab/<pkg>`` symlinks into
``<sdk>/packages/<pkg>``, with ``node_modules/`` ignored).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import hashlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "scripts" / "build-image.sh"
VERIFY = REPO / "scripts" / "verify-image.sh"
DOCKERFILE = REPO / "docker" / "Dockerfile.appliance"

EIGHT_ARGS = {
    "CANOPY_SHA",
    "RUNTIME_SHA",
    "TRACE_SHA",
    "TRACE_LOCK_SHA256",
    "CANOPY_VERSION",
    "PYDM_VERSION",
    "SDK_SHA",
    "PYDM_SHA",
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

    pydm = make_repo(
        tmp_path / "pydm",
        {"pyproject.toml": "[project]\nname='PyDMConverter'\n", "pydmconverter/__init__.py": ""},
    )
    sdk = make_repo(
        tmp_path / "sdk",
        {
            "package.json": "{}\n",
            "packages/screen-ir/package.json": '{"name": "@slaclab/canopy-screen-ir"}\n',
            "packages/client/package.json": '{"name": "@slaclab/canopy-client"}\n',
        },
    )
    rt = make_repo(
        tmp_path / "rt",
        {".gitignore": "node_modules/\n", "package.json": "{}\n", "apps/viewer/package.json": "{}\n"},
    )
    link_dir = rt / "apps" / "viewer" / "node_modules" / "@slaclab"
    link_dir.mkdir(parents=True)
    for pkg, sub in (("canopy-screen-ir", "screen-ir"), ("canopy-client", "client")):
        (link_dir / pkg).symlink_to(os.path.relpath(sdk / "packages" / sub, link_dir))
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
        if k not in {"PYDM", "RT", "SDK", "TRACE", "TRACE_REF", "TAG", "PLATFORM", "CANOPY_VERSION", "PYDM_VERSION"}
    }
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TMPDIR"] = str(tmp_path)
    env.update(
        PYDM=str(pydm),
        RT=str(rt),
        SDK=str(sdk),
        TRACE=str(trace),
        TRACE_REF="feature",
        CANOPY_VERSION="1.2.3",
        PYDM_VERSION="4.5.6",
    )
    return {
        "can": can,
        "pydm": pydm,
        "rt": rt,
        "sdk": sdk,
        "trace": trace,
        "env": env,
        "marker": marker,
        "bin": bin_dir,
    }


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
    for name in ("PYDM", "RT", "SDK", "TRACE", "TRACE_REF", "TAG", "PLATFORM", "--no-trace", "--dry-run"):
        assert name in proc.stdout


# --- build-image.sh: the argv ------------------------------------------------------


def test_argv_carries_all_build_args_and_contexts(world):
    proc = dry(world)
    argv = ok_argv(proc)
    assert argv[:2] == ["docker", "build"]
    args = pairs(argv, "--build-arg")
    assert set(args) == EIGHT_ARGS
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
    assert args["SDK_SHA"] == head(world["sdk"])
    assert args["PYDM_SHA"] == head(world["pydm"])
    assert trace_sha in proc.stderr  # resolved SHA is printed


def test_canopy_dirty_only_for_tracked_changes(world):
    (world["can"] / "untracked.txt").write_text("x\n")
    args = pairs(ok_argv(dry(world)), "--build-arg")
    assert args["CANOPY_SHA"] == head(world["can"])
    assert args["RUNTIME_SHA"] == head(world["rt"])

    (world["can"] / "docker" / "Dockerfile.appliance").write_text(DOCKERFILE.read_text() + "\n")
    args = pairs(ok_argv(dry(world)), "--build-arg")
    assert args["CANOPY_SHA"] == head(world["can"]) + "+dirty"


@pytest.mark.parametrize("which", ["rt", "sdk"])
@pytest.mark.parametrize("change", ["untracked", "tracked"])
def test_dirty_rt_or_sdk_is_refused(world, which, change):
    repo = world[which]
    if change == "untracked":
        (repo / "untracked.txt").write_text("x\n")
    else:
        (repo / "package.json").write_text('{"x": 1}\n')
    proc = dry(world)
    assert proc.returncode != 0
    assert which.upper() in proc.stderr and "clean" in proc.stderr
    assert_no_tool_called(world)


def test_untracked_pydm_wheel_input_is_refused(world):
    (world["pydm"] / "pydmconverter" / "x.py").write_text("x = 1\n")
    proc = dry(world)
    assert proc.returncode != 0
    assert "PYDM" in proc.stderr and "pydmconverter/x.py" in proc.stderr
    assert_no_tool_called(world)


def test_untracked_pydm_pyproject_is_refused(world):
    git(world["pydm"], "rm", "-q", "--cached", "pyproject.toml")
    git(world["pydm"], "commit", "-q", "-m", "untrack pyproject")
    proc = dry(world)
    assert proc.returncode != 0
    assert "pyproject.toml" in proc.stderr


def test_untracked_pydm_lockfile_passes(world):
    (world["pydm"] / "uv.lock").write_text("version = 1\n")
    (world["pydm"] / "notes.txt").write_text("x\n")
    args = pairs(ok_argv(dry(world)), "--build-arg")
    assert args["PYDM_SHA"] == head(world["pydm"])


def test_sdk_derived_from_the_runtime_link_when_unset(world):
    proc = dry(world, unset=("SDK",))
    args = pairs(ok_argv(proc), "--build-arg")
    assert args["SDK_SHA"] == head(world["sdk"])
    assert str(world["sdk"].resolve()) in proc.stderr
    assert_no_tool_called(world)


def test_sdk_mismatch_is_refused(world, tmp_path):
    other = make_repo(tmp_path / "other-sdk", {"packages/screen-ir/package.json": "{}\n"})
    proc = dry(world, SDK=str(other))
    assert proc.returncode != 0
    assert "SDK" in proc.stderr and "link" in proc.stderr
    assert_no_tool_called(world)


def test_sdk_given_through_a_symlink_matches(world, tmp_path):
    alias = tmp_path / "sdk-alias"
    alias.symlink_to(world["sdk"])
    args = pairs(ok_argv(dry(world, SDK=str(alias))), "--build-arg")
    assert args["SDK_SHA"] == head(world["sdk"])


def test_missing_runtime_link_is_refused(world):
    (world["rt"] / "apps" / "viewer" / "node_modules" / "@slaclab" / "canopy-screen-ir").unlink()
    for unset in ((), ("SDK",)):
        proc = dry(world, unset=unset)
        assert proc.returncode != 0
        assert "canopy-screen-ir" in proc.stderr
    assert_no_tool_called(world)


def test_client_link_into_another_sdk_is_refused(world, tmp_path):
    other = make_repo(tmp_path / "other-sdk", {"packages/client/package.json": "{}\n"})
    link = world["rt"] / "apps" / "viewer" / "node_modules" / "@slaclab" / "canopy-client"
    link.unlink()
    link.symlink_to(other / "packages" / "client")
    proc = dry(world)
    assert proc.returncode != 0
    assert "canopy-client" in proc.stderr


def test_no_trace_passes_with_empty_trace_contexts(world):
    proc = dry(world, "--no-trace", unset=("TRACE", "TRACE_REF"))
    argv = ok_argv(proc)
    contexts = pairs(argv, "--build-context")
    assert {"trace-src", "trace"} <= set(contexts)
    args = pairs(argv, "--build-arg")
    assert set(args) == EIGHT_ARGS
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


def final_stage(text: str) -> str:
    return text[text.rindex("\nFROM ") :]


def test_sdk_and_pydm_labels_and_args_in_the_final_stage():
    final = final_stage(DOCKERFILE.read_text())
    declared = set(re.findall(r"^ARG (\w+)", final, re.M))
    assert {"SDK_SHA", "PYDM_SHA"} <= declared
    assert 'org.canopy.sdk.sha="${SDK_SHA}"' in final
    assert 'org.canopy.pydm.sha="${PYDM_SHA}"' in final


def test_dockerfile_smoke_step_after_install():
    final = final_stage(DOCKERFILE.read_text())
    install = final.index("pip install")
    assert final.index("import pydmconverter.scheme") > install
    assert final.index("converter/data/schemes/canopy-default.json") > install


def test_real_build_builds_the_linked_sdk_then_the_viewer_closure():
    text = BUILD.read_text()
    sdk_build = text.index("@slaclab/canopy-screen-ir")
    assert "@slaclab/canopy-client" in text
    viewer = text.index("pnpm --filter 'viewer...' build")
    assert text.rindex("@slaclab/canopy-client") < viewer and sdk_build < viewer
    assert "pnpm --filter viewer build" not in text


def test_no_uv_sources_pin():
    text = BUILD.read_text()
    assert "tool.uv.sources" not in text and "uv add" not in text


# --- verify-image.sh: usage ------------------------------------------------------


@pytest.mark.parametrize("var", ["TRACE_REF", "TRACE", "RT", "SDK", "PYDM"])
def test_verify_without_input_is_usage_error(world, var):
    proc = run(world, "verify-image.sh", "canopy:dev", unset=(var,))
    assert proc.returncode == 2
    assert var in proc.stderr and "Usage" in proc.stderr
    assert_no_tool_called(world)


def test_verify_without_image_is_usage_error(world):
    proc = run(world, "verify-image.sh")
    assert proc.returncode == 2
    assert_no_tool_called(world)


def test_verify_help_lists_sdk_and_pydm(world):
    proc = run(world, "verify-image.sh", "--help")
    assert proc.returncode == 0
    assert "SDK" in proc.stdout and "PYDM" in proc.stdout


# --- verify-image.sh: label comparison against a fake docker ---------------------

FAKE_DOCKER = """#!/bin/sh
# image inspect [-f '{{ index .Config.Labels "KEY" }}'] IMAGE -> KEY's value from $LABELS
if [ "$1" = image ] && [ "$2" = inspect ]; then
    if [ "$3" = -f ]; then
        key=$(printf '%s' "$4" | sed 's/.*"\\(.*\\)".*/\\1/')
        grep "^$key=" "$LABELS" | cut -d= -f2-
    fi
    exit 0
fi
[ "$1" = run ] && exit 0
exit 97
"""


def verify_labels(world: dict, tmp_path: Path, **override: str):
    labels = {
        "org.canopy.sha": head(world["can"]),
        "org.canopy.runtime.sha": head(world["rt"]),
        "org.canopy.trace.sha": git(world["trace"], "rev-parse", "feature"),
        "org.canopy.trace.lockfile-sha256": hashlib.sha256(LOCK_TEXT.encode()).hexdigest(),
        "org.canopy.sdk.sha": head(world["sdk"]),
        "org.canopy.pydm.sha": head(world["pydm"]),
    }
    labels.update({k.replace("_", "."): v for k, v in override.items()})
    path = tmp_path / "labels"
    path.write_text("".join(f"{k}={v}\n" for k, v in labels.items()))
    docker = world["bin"] / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return run(world, "verify-image.sh", "canopy:test", LABELS=str(path))


def test_verify_accepts_matching_sdk_and_pydm_labels(world, tmp_path):
    proc = verify_labels(world, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok   org.canopy.sdk.sha" in proc.stdout
    assert "ok   org.canopy.pydm.sha" in proc.stdout


@pytest.mark.parametrize("label", ["org_canopy_sdk_sha", "org_canopy_pydm_sha"])
def test_verify_refuses_a_mismatched_sdk_or_pydm_label(world, tmp_path, label):
    proc = verify_labels(world, tmp_path, **{label: "0" * 40})
    assert proc.returncode == 1
    assert f"FAIL {label.replace('_', '.')}" in proc.stderr


def test_verify_accepts_a_working_tree_sdk_label(world, tmp_path):
    (world["sdk"] / "untracked.txt").write_text("x\n")
    git(world["trace"], "checkout", "-q", "feature")
    sdk_label = pairs(ok_argv(dry(world, "--worktree")), "--build-arg")["SDK_SHA"]
    assert FP.fullmatch(sdk_label.removeprefix(head(world["sdk"])))
    proc = verify_labels(world, tmp_path, org_canopy_sdk_sha=sdk_label)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (world["sdk"] / "untracked.txt").write_text("y\n")
    proc = verify_labels(world, tmp_path, org_canopy_sdk_sha=sdk_label)
    assert proc.returncode == 1


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


def test_worktree_is_exempt_from_the_clean_tree_refusals(world):
    git(world["trace"], "checkout", "-q", "feature")
    (world["rt"] / "untracked.txt").write_text("x\n")
    (world["sdk"] / "package.json").write_text('{"x": 1}\n')
    (world["pydm"] / "pydmconverter" / "x.py").write_text("x = 1\n")
    args = pairs(ok_argv(dry(world, "--worktree")), "--build-arg")
    assert FP.fullmatch(args["RUNTIME_SHA"].removeprefix(head(world["rt"])))
    assert FP.fullmatch(args["SDK_SHA"].removeprefix(head(world["sdk"])))
    assert args["PYDM_SHA"] == head(world["pydm"])  # the wheel is still built from HEAD
    assert_no_tool_called(world)


# --- build-image.sh --worktree: link: dependencies of Trace ------------------------


def link_trace(world: dict, **targets: Path) -> None:
    """Give Trace's working tree one ``link:`` dependency per ``name=<dir>``."""
    git(world["trace"], "checkout", "-q", "feature")
    deps = ", ".join(
        f'"@slaclab/{name}": "link:{os.path.relpath(target, world["trace"])}"' for name, target in targets.items()
    )
    (world["trace"] / "package.json").write_text(f'{{"dependencies": {{{deps}}}}}\n')


def add_shell_package(sdk: Path) -> Path:
    shell = sdk / "packages" / "shell"
    shell.mkdir(parents=True)
    (shell / "package.json").write_text('{"name": "@slaclab/canopy-shell"}\n')
    git(sdk, "add", "-A")
    git(sdk, "commit", "-q", "-m", "shell")
    return shell


def test_two_links_into_one_sdk_root_accepted(world):
    sdk = world["sdk"]
    shell = add_shell_package(sdk)
    link_trace(world, **{"canopy-client": sdk / "packages" / "client", "canopy-shell": shell})

    proc = dry(world, "--worktree")
    argv = ok_argv(proc)
    # Host-built Trace: its dist is the `trace` context, labelled with the SDK root's commit.
    assert pairs(argv, "--build-context")["trace"] == str(world["trace"] / "dist")
    assert pairs(argv, "--label") == {"org.canopy.trace.sdk.sha": head(sdk)}
    # The linked SDK is the root both packages live in, not either package.
    assert f"({sdk.resolve()}, linked from TRACE)" in proc.stderr

    # Uncommitted work anywhere in that root (not only in a linked package) moves the label.
    (sdk / "packages" / "screen-ir" / "new.ts").write_text("export {}\n")
    label = pairs(ok_argv(dry(world, "--worktree")), "--label")["org.canopy.trace.sdk.sha"]
    assert FP.fullmatch(label.removeprefix(head(sdk)))
    assert_no_tool_called(world)

    # The real build builds the whole root, so every linked package's dist is fresh.
    text = BUILD.read_text()
    assert '(cd "$TRACE_SDK_DIR" && unset TRACE && pnpm run build)' in text


def test_links_into_two_sdk_roots_refused(world, tmp_path):
    other = make_repo(
        tmp_path / "sdk2",
        {"package.json": "{}\n", "packages/shell/package.json": '{"name": "@slaclab/canopy-shell"}\n'},
    )
    link_trace(
        world,
        **{"canopy-client": world["sdk"] / "packages" / "client", "canopy-shell": other / "packages" / "shell"},
    )

    proc = dry(world, "--worktree")
    assert proc.returncode != 0
    assert "different SDK roots" in proc.stderr
    assert str(world["sdk"].resolve()) in proc.stderr
    assert str(other.resolve()) in proc.stderr
    assert_no_tool_called(world)
