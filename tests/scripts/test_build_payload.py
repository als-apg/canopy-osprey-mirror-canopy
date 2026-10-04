"""scripts/build-payload.sh against throwaway checkouts.

Five fake repositories stand in for canopy, canopy-screen-runtime,
canopy-sdk-js, pydm-converter-tool and react-trace. ``pnpm``, ``uv`` and
``uvx`` are PATH stubs that log every call (tool, working directory, argv) and
write the artifact the real tool would, built from the files they find in
their working directory, so the payload's content shows which tree was built.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build-payload.sh"
LEAF = Path(".claude/worktrees/canopy-osprey-connectors")
# The runtime's link to the SDK, as committed in canopy-screen-runtime: six
# levels up from packages/runtime is the root that holds every repo directory.
SDK_LINK = "../../../../../../canopy-sdk-js/.claude/worktrees/canopy-osprey-connectors"

UV_STUB = r"""#!/bin/sh
printf 'uv\t%s\t%s\n' "$PWD" "$*" >> "$STUB_LOG"
[ "$1 $2" = "build --wheel" ] || exit 97
[ -z "${STUB_FAIL_UV:-}" ] || exit 1
out="$4"
if grep -q PyDMConverter pyproject.toml; then
    name=pydmconverter; version="${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYDMCONVERTER:-unset}"
else
    name=canopy; version="${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CANOPY:-unset}"
    [ "${SETUPTOOLS_SCM_PRETEND_VERSION:-unset}" = "$version" ] || exit 96
fi
cat marker.txt > "$out/$name-$version-py3-none-any.whl"
ls > "$out/$name.files"
"""

UVX_STUB = r"""#!/bin/sh
printf 'uvx\t%s\t%s\n' "$PWD" "$*" >> "$STUB_LOG"
if grep -q PyDMConverter pyproject.toml; then echo 4.5.6; else echo 1.2.3.dev4+gabc1234; fi
"""

PNPM_STUB = r"""#!/bin/sh
printf 'pnpm\t%s\t%s\tTRACE=%s HUSKY=%s\n' "$PWD" "$*" "${TRACE:-unset}" "${HUSKY:-unset}" >> "$STUB_LOG"
case "$*" in
    "install --frozen-lockfile")
        # Only the fake Trace has a lockfile: it may declare the SDK client itself.
        if [ -n "${STUB_OWN_CLIENT:-}" ] && [ -f pnpm-lock.yaml ]; then
            mkdir -p node_modules/@slaclab/canopy-client
            echo "from the lockfile" > node_modules/@slaclab/canopy-client/own.txt
        fi
        ;;
    "run build")
        if [ -d apps/viewer ]; then
            [ -z "${STUB_FAIL_VIEWER:-}" ] || exit 1
            mkdir -p apps/viewer/dist
            {
                cat marker.txt
                # What the runtime's link: reaches: the SDK export's build output.
                cat "packages/runtime/$SDK_LINK/dist/built.txt"
            } > apps/viewer/dist/index.html || exit 95
            cp packages/runtime/ir-map.json apps/viewer/dist/ir-map.json
        else
            mkdir -p dist
            cat marker.txt > dist/built.txt
        fi
        ;;
    "exec tsc -b") ;;
    "exec vite build --base=./")
        mkdir -p dist
        cat marker.txt > dist/index.html
        # Where the SDK client resolves: its own copy, or two levels above a
        # linked packages/client, the SDK tree's marker.
        client=node_modules/@slaclab/canopy-client
        if [ -f "$client/own.txt" ]; then cat "$client/own.txt" > dist/sdk.txt
        elif [ -e "$client" ]; then cat "$client/../../marker.txt" > dist/sdk.txt
        fi
        ;;
    *) exit 97 ;;
esac
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(path: Path, files: dict[str, str]) -> Path:
    """A repo with two commits; ``marker.txt`` reads ``<name> v1`` then ``<name> v2``."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    commit(path, {**files, "marker.txt": f"{path.name} v1\n"}, "one")
    commit(path, {"marker.txt": f"{path.name} v2\n"}, "two")
    return path


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def first(path: Path) -> str:
    return git(path, "rev-parse", "HEAD~1")


@pytest.fixture
def world(tmp_path: Path) -> dict:
    src = tmp_path / "src"
    canopy = src / "canopy"
    (canopy / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, canopy / "scripts" / "build-payload.sh")
    make_canopy = {
        "pyproject.toml": "[project]\nname='canopy'\n",
        "widget-registry-data/widgets/arc.json": '{"id": "arc"}\n',
        "widget-registry-data/widgets/frame.json": '{"id": "frame"}\n',
        "widget-registry-data/icons/arc.svg": "<svg/>\n",
    }
    git(canopy, "init", "-q", "-b", "main")
    commit(canopy, {**make_canopy, "marker.txt": "canopy v1\n"}, "one")
    commit(canopy, {"marker.txt": "canopy v2\n"}, "two")

    pydm = make_repo(src / "pydm-converter-tool", {"pyproject.toml": "[project]\nname='PyDMConverter'\n"})
    sdk = make_repo(
        src / "canopy-sdk-js",
        {"package.json": "{}\n", "packages/client/package.json": '{\n  "name": "@slaclab/canopy-client"\n}\n'},
    )
    rt = make_repo(
        src / "canopy-screen-runtime",
        {
            "package.json": "{}\n",
            "apps/viewer/package.json": "{}\n",
            "packages/runtime/ir-map.json": '{"arc": {}}\n',
        },
    )
    trace = make_repo(src / "react-trace", {"package.json": "{}\n", "pnpm-lock.yaml": "lock\n"})
    git(trace, "branch", "feature", "HEAD~1")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool, text in (("uv", UV_STUB), ("uvx", UVX_STUB), ("pnpm", PNPM_STUB)):
        stub = bin_dir / tool
        stub.write_text(text)
        stub.chmod(0o755)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    log = tmp_path / "stub.log"
    dropped = {
        "PYDM", "RT", "SDK", "TRACE", "TRACE_REF", "CANOPY_SHA", "RT_SHA", "SDK_SHA", "PYDM_SHA",
        "CANOPY_VERSION", "PYDM_VERSION", "HUSKY",
    }  # fmt: skip
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TMPDIR"] = str(scratch)
    env.update(
        PYDM=str(pydm),
        RT=str(rt),
        SDK=str(sdk),
        TRACE=str(trace),
        TRACE_REF="main",
        STUB_LOG=str(log),
        SDK_LINK=SDK_LINK,
    )
    return {
        "canopy": canopy,
        "pydm": pydm,
        "sdk": sdk,
        "rt": rt,
        "trace": trace,
        "repos": [canopy, pydm, sdk, rt, trace],
        "env": env,
        "log": log,
        "scratch": scratch,
        "out": tmp_path / "out",
    }


def build(world: dict, *args: str, unset: tuple[str, ...] = (), out: Path | bool | None = None, **extra: str):
    env = {k: v for k, v in world["env"].items() if k not in unset}
    env.update(extra)
    argv = ["bash", str(world["canopy"] / "scripts" / "build-payload.sh"), *args]
    if out is not False:
        argv.append(str(out or world["out"]))
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)


def ok(proc) -> None:
    assert proc.returncode == 0, proc.stdout + proc.stderr


def calls(world: dict) -> list[tuple[str, Path, str, str]]:
    """Logged stub calls as (tool, cwd, argv, env note)."""
    if not world["log"].exists():
        return []
    rows = []
    for line in world["log"].read_text().splitlines():
        tool, cwd, argv, *rest = line.split("\t")
        rows.append((tool, Path(cwd), argv, rest[0] if rest else ""))
    return rows


def staged(world: dict) -> dict:
    return json.loads((world["out"] / "STAGED.json").read_text())


# --- the payload ---------------------------------------------------------------


def test_payload_holds_wheels_viewer_trace_widgets_and_staged(world):
    ok(build(world))
    out = world["out"]
    assert sorted(p.name for p in out.glob("*.whl")) == [
        "canopy-1.2.3.dev4+gabc1234-py3-none-any.whl",
        "pydmconverter-4.5.6-py3-none-any.whl",
    ]
    assert (out / "viewer" / "index.html").read_text().startswith("canopy-screen-runtime v2\n")
    assert json.loads((out / "viewer" / "ir-map.json").read_text()) == {"arc": {}}
    assert (out / "trace" / "index.html").read_text() == "react-trace v2\n"
    assert sorted(p.name for p in (out / "widgets").iterdir()) == ["arc.json", "frame.json"]
    assert (out / "STAGED.json").is_file()


def test_staged_names_five_commits_and_both_versions(world):
    ok(build(world))
    assert staged(world) == {
        "canopy": head(world["canopy"]),
        "runtime": head(world["rt"]),
        "sdk": head(world["sdk"]),
        "pydm": head(world["pydm"]),
        "trace": head(world["trace"]),
        "canopy_version": "1.2.3.dev4+gabc1234",
        "pydm_version": "4.5.6",
    }


def test_trace_ref_is_resolved_to_its_commit(world):
    ok(build(world, TRACE_REF="feature"))
    assert staged(world)["trace"] == first(world["trace"])
    assert (world["out"] / "trace" / "index.html").read_text() == "react-trace v1\n"


def test_recorded_shas_are_built_instead_of_head(world):
    shas = {
        "CANOPY_SHA": first(world["canopy"]),
        "RT_SHA": first(world["rt"]),
        "SDK_SHA": first(world["sdk"]),
        "PYDM_SHA": first(world["pydm"]),
    }
    ok(build(world, **shas))
    out = world["out"]
    s = staged(world)
    assert (s["canopy"], s["runtime"], s["sdk"], s["pydm"]) == tuple(shas.values())
    assert s["trace"] == head(world["trace"])
    assert (out / "canopy-1.2.3.dev4+gabc1234-py3-none-any.whl").read_text() == "canopy v1\n"
    assert (out / "pydmconverter-4.5.6-py3-none-any.whl").read_text() == "pydm-converter-tool v1\n"
    assert (out / "viewer" / "index.html").read_text() == "canopy-screen-runtime v1\ncanopy-sdk-js v1\n"


def test_short_sha_is_recorded_in_full(world):
    full = first(world["rt"])
    ok(build(world, RT_SHA=full[:10]))
    assert staged(world)["runtime"] == full


# --- detached exports ---------------------------------------------------------------


def test_builds_run_only_in_exports_under_one_root_laid_out_as_bw4(world):
    ok(build(world))
    builds = [(tool, cwd, argv) for tool, cwd, argv, _ in calls(world) if tool != "uvx"]
    assert builds
    roots = set()
    seen = set()
    for _tool, cwd, _argv in builds:
        assert cwd.parts[-3:] == LEAF.parts, cwd
        repo_dir = cwd.parents[2]
        seen.add(repo_dir.name)
        roots.add(repo_dir.parent)
    assert seen == {r.name for r in world["repos"]}
    assert len(roots) == 1
    root = roots.pop()
    assert world["scratch"].resolve() in root.parents
    assert not any(src in cwd.parents or src == cwd for _, cwd, _ in builds for src in world["repos"])


def test_runtime_link_reaches_the_built_sdk_export(world):
    ok(build(world))
    # The stub's viewer build reads the SDK's build output through the relative link.
    assert (world["out"] / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\ncanopy-sdk-js v2\n"
    order = [(cwd.parents[2].name, argv) for tool, cwd, argv, _ in calls(world) if tool == "pnpm"]
    assert order.index(("canopy-sdk-js", "run build")) < order.index(("canopy-screen-runtime", "run build"))
    assert ("canopy-screen-runtime", "install --frozen-lockfile") in order
    assert ("canopy-sdk-js", "install --frozen-lockfile") in order


def test_trace_is_built_with_the_image_steps(world):
    ok(build(world))
    trace = [argv for tool, cwd, argv, _ in calls(world) if tool == "pnpm" and cwd.parents[2].name == "react-trace"]
    assert trace == ["install --frozen-lockfile", "exec tsc -b", "exec vite build --base=./"]


def test_trace_resolves_an_undeclared_sdk_package_from_the_sdk_export(world):
    ok(build(world, SDK_SHA=first(world["sdk"])))
    assert (world["out"] / "trace" / "sdk.txt").read_text() == "canopy-sdk-js v1\n"
    order = [(cwd.parents[2].name, argv) for tool, cwd, argv, _ in calls(world) if tool == "pnpm"]
    assert order.index(("canopy-sdk-js", "run build")) < order.index(("react-trace", "exec tsc -b"))


def test_trace_keeps_an_sdk_package_its_own_install_provides(world):
    ok(build(world, STUB_OWN_CLIENT="1"))
    assert (world["out"] / "trace" / "sdk.txt").read_text() == "from the lockfile\n"


def test_pnpm_never_sees_trace_and_husky_is_off(world):
    ok(build(world))
    notes = {note for tool, _, _, note in calls(world) if tool == "pnpm"}
    assert notes == {"TRACE=unset HUSKY=0"}


def test_uncommitted_edits_are_absent_from_the_payload(world):
    for repo in world["repos"]:
        (repo / "marker.txt").write_text(f"{repo.name} EDITED\n")  # tracked, modified
        (repo / "untracked.txt").write_text("x\n")
    (world["canopy"] / "widget-registry-data" / "widgets" / "draft.json").write_text("{}\n")
    (world["canopy"] / "widget-registry-data" / "widgets" / "arc.json").write_text('{"id": "EDITED"}\n')
    (world["rt"] / "packages" / "runtime" / "ir-map.json").write_text('{"EDITED": {}}\n')
    git(world["sdk"], "add", "-A")  # staged, not committed

    ok(build(world))
    out = world["out"]
    texts = [p.read_text() for p in out.rglob("*") if p.is_file()]
    assert not any("EDITED" in t for t in texts)
    assert sorted(p.name for p in (out / "widgets").iterdir()) == ["arc.json", "frame.json"]
    assert "untracked.txt" not in (out / "canopy.files").read_text()
    assert "untracked.txt" not in (out / "pydmconverter.files").read_text()
    assert staged(world)["canopy"] == head(world["canopy"])


def test_checkouts_are_left_untouched(world):
    before = {r: git(r, "status", "--porcelain", "--ignored") for r in world["repos"]}
    ok(build(world))
    assert {r: git(r, "status", "--porcelain", "--ignored") for r in world["repos"]} == before
    assert not (world["rt"] / "apps" / "viewer" / "dist").exists()
    assert not (world["trace"] / "dist").exists()


def test_temp_root_is_removed(world):
    ok(build(world))
    assert list(world["scratch"].iterdir()) == []
    assert build(world, out=world["out"].parent / "second", STUB_FAIL_VIEWER="1").returncode != 0
    assert list(world["scratch"].iterdir()) == []


# --- versions ----------------------------------------------------------------------


def test_versions_come_from_the_source_checkouts_and_are_pretended(world):
    ok(build(world))
    scm = [(cwd, argv) for tool, cwd, argv, _ in calls(world) if tool == "uvx"]
    assert [cwd for cwd, _ in scm] == [world["canopy"].resolve(), world["pydm"]]
    assert all("setuptools_scm" in argv for _, argv in scm)
    # The uv stub names each wheel after the pretended version it was handed.
    names = sorted(p.name for p in world["out"].glob("*.whl"))
    assert names == ["canopy-1.2.3.dev4+gabc1234-py3-none-any.whl", "pydmconverter-4.5.6-py3-none-any.whl"]
    first_build = next(i for i, c in enumerate(calls(world)) if c[0] != "uvx")
    assert all(c[0] != "uvx" for c in calls(world)[first_build:])  # versions before any export is built


def test_given_versions_are_used_as_they_are(world):
    ok(build(world, CANOPY_VERSION="7.0.0", PYDM_VERSION="8.0.0"))
    assert not [c for c in calls(world) if c[0] == "uvx"]
    s = staged(world)
    assert (s["canopy_version"], s["pydm_version"]) == ("7.0.0", "8.0.0")
    assert (world["out"] / "canopy-7.0.0-py3-none-any.whl").is_file()
    assert (world["out"] / "pydmconverter-8.0.0-py3-none-any.whl").is_file()


# --- --no-trace --------------------------------------------------------------------


def test_no_trace_needs_no_trace_checkout(world):
    ok(build(world, "--no-trace", unset=("TRACE", "TRACE_REF")))
    out = world["out"]
    assert staged(world)["trace"] is None
    assert (out / "trace").is_dir() and list((out / "trace").iterdir()) == []
    assert (out / "viewer" / "index.html").is_file()
    assert not [c for c in calls(world) if "vite" in c[2]]


# --- refusals ----------------------------------------------------------------------


@pytest.mark.parametrize("var", ["PYDM", "RT", "SDK", "TRACE", "TRACE_REF"])
def test_missing_input_fails_before_any_tool_runs(world, var):
    proc = build(world, unset=(var,))
    assert proc.returncode != 0
    assert var in proc.stderr
    assert calls(world) == []
    assert not world["out"].exists()


def test_missing_out_is_a_usage_error(world):
    proc = build(world, out=False)
    assert proc.returncode == 2
    assert "Usage" in proc.stderr


def test_unknown_option_is_a_usage_error(world):
    assert build(world, "--bogus").returncode == 2


def test_help_lists_inputs(world):
    proc = build(world, "--help", out=False, unset=("PYDM", "RT", "SDK", "TRACE", "TRACE_REF"))
    assert proc.returncode == 0
    for name in (
        "PYDM",
        "RT",
        "SDK",
        "TRACE",
        "TRACE_REF",
        "CANOPY_SHA",
        "RT_SHA",
        "SDK_SHA",
        "PYDM_SHA",
        "--no-trace",
    ):
        assert name in proc.stdout


@pytest.mark.parametrize("var", ["CANOPY_SHA", "RT_SHA", "SDK_SHA", "PYDM_SHA", "TRACE_REF"])
def test_unresolvable_commit_fails_naming_the_input(world, var):
    proc = build(world, **{var: "0123456789abcdef0123456789abcdef01234567"})
    assert proc.returncode != 0
    assert var in proc.stderr
    assert calls(world) == []


def test_non_empty_out_is_refused(world):
    world["out"].mkdir()
    (world["out"] / "stale.whl").write_text("old\n")
    proc = build(world)
    assert proc.returncode != 0
    assert "not empty" in proc.stderr
    assert calls(world) == []


def test_existing_empty_out_is_filled(world):
    world["out"].mkdir()
    ok(build(world))
    assert (world["out"] / "STAGED.json").is_file()


@pytest.mark.parametrize("failure", ["STUB_FAIL_UV", "STUB_FAIL_VIEWER"])
def test_failed_build_leaves_no_payload(world, failure):
    proc = build(world, **{failure: "1"})
    assert proc.returncode != 0
    assert not (world["out"] / "STAGED.json").exists()
    assert not world["out"].exists() or list(world["out"].iterdir()) == []


def test_checkouts_sharing_a_directory_name_are_refused(world, tmp_path):
    twin = make_repo(tmp_path / "other" / "canopy-sdk-js", {"package.json": "{}\n"})
    proc = build(world, TRACE=str(twin), TRACE_REF="main")
    assert proc.returncode != 0
    assert "canopy-sdk-js" in proc.stderr
    assert calls(world) == []


def test_a_linked_worktree_exports_under_its_repository_name(world, tmp_path):
    linked = tmp_path / "elsewhere" / "wt"
    linked.parent.mkdir()
    git(world["sdk"], "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    ok(build(world, SDK=str(linked)))
    assert (world["out"] / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\ncanopy-sdk-js v2\n"
