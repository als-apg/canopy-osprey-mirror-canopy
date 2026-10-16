"""scripts/build-payload.sh against throwaway checkouts.

Five fake repositories stand in for canopy, canopy-screen-runtime,
canopy-sdk-js, pydm-converter-tool and react-trace. ``pnpm``, ``uv`` and
``uvx`` are PATH stubs that log every call (tool, working directory, argv) and
write the artifact the real tool would, built from the files they find in
their working directory, so the payload's content shows which tree was built.
The runtime and Trace each pin an SDK commit at ``vendor/canopy-sdk-js``.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build-payload.sh"
# Where canopy-screen-runtime and react-trace vendor the SDK: a submodule whose
# gitlink names the SDK commit each is built against.
SDK_VENDOR = "vendor/canopy-sdk-js"

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

# Like setuptools_scm, the version names the checkout's HEAD as ``+g<7 hex>``;
# STUB_SCM_VERSION replaces it outright.
UVX_STUB = r"""#!/bin/sh
printf 'uvx\t%s\t%s\n' "$PWD" "$*" >> "$STUB_LOG"
[ -z "${STUB_SCM_VERSION:-}" ] || { echo "$STUB_SCM_VERSION"; exit 0; }
node="$(git rev-parse HEAD | cut -c1-7)"
if grep -q PyDMConverter pyproject.toml; then echo "4.5.6+g$node"; else echo "1.2.3.dev4+g$node"; fi
"""

PNPM_STUB = r"""#!/bin/sh
printf 'pnpm\t%s\t%s\tTRACE=%s HUSKY=%s\n' "$PWD" "$*" "${TRACE:-unset}" "${HUSKY:-unset}" >> "$STUB_LOG"
case "$*" in
    "install --frozen-lockfile") ;;
    "run build")
        [ -d apps/viewer ] || exit 97
        [ -z "${STUB_FAIL_VIEWER:-}" ] || exit 1
        mkdir -p apps/viewer/dist
        {
            cat marker.txt
            # The SDK tree the runtime vendors, as the build finds it.
            cat vendor/canopy-sdk-js/marker.txt
        } > apps/viewer/dist/index.html || exit 95
        cp packages/runtime/ir-map.json apps/viewer/dist/ir-map.json
        ;;
    "exec tsc -b") ;;
    "exec vite build --base=./")
        mkdir -p dist
        cat marker.txt > dist/index.html
        # The SDK tree Trace vendors, as the build finds it.
        cat vendor/canopy-sdk-js/marker.txt > dist/sdk.txt || exit 95
        ;;
    "run check-dist")
        [ -z "${STUB_FAIL_CHECK_DIST:-}" ] || exit 1
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


def sdk_pin(repo: Path) -> str | None:
    """The SDK commit ``repo``'s HEAD pins at ``vendor/canopy-sdk-js``, if it pins one."""
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=repo, capture_output=True).returncode:
        return None
    entry = git(repo, "ls-tree", "HEAD", "--", SDK_VENDOR).split()
    return entry[2] if entry[:2] == ["160000", "commit"] else None


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    """Commit ``files``; a pinned SDK commit carries over (``add -A`` drops a gitlink with no directory)."""
    return commit_pinning(repo, files, sdk_pin(repo), message)


def commit_pinning(repo: Path, files: dict[str, str], sdk_sha: str | None, message: str) -> str:
    """A commit whose ``vendor/canopy-sdk-js`` gitlink names ``sdk_sha`` (no gitlink for None)."""
    write(repo, files)
    git(repo, "add", "-A")
    if sdk_sha is None:
        git(repo, "rm", "-q", "--cached", "--ignore-unmatch", SDK_VENDOR)
    else:
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sdk_sha},{SDK_VENDOR}")
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


def canopy_version(world: dict) -> str:
    """The version the uvx stub computes for the canopy checkout's HEAD."""
    return f"1.2.3.dev4+g{head(world['canopy'])[:7]}"


def pydm_version(world: dict) -> str:
    return f"4.5.6+g{head(world['pydm'])[:7]}"


# The runtime's ir-map.json carries the editor metadata the catalog is built from.
IR_MAP = '{"arc": {"consumes": [], "verbatim": true, "displayName": "Arc"}}\n'
SPEC_SHA = "0123456789abcdef0123456789abcdef01234567"


def make_spec(root: Path, schema: str = "{}\n") -> Path:
    """A staged spec directory in the epic layout."""
    write(
        root,
        {
            "SPEC-SOURCE": f"repo: slaclab/canopy-spec\nsha: {SPEC_SHA}\n",
            "ir/screen-ir-1.1.schema.json": schema,
            "widgets/arc.props.schema.json": '{"type": "object"}\n',
        },
    )
    return root


def wheel_names(world: dict) -> list[str]:
    return [
        f"canopy-{canopy_version(world)}-py3-none-any.whl",
        f"pydmconverter-{pydm_version(world)}-py3-none-any.whl",
    ]


@pytest.fixture
def world(tmp_path: Path) -> dict:
    src = tmp_path / "src"
    canopy = src / "canopy"
    (canopy / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, canopy / "scripts" / "build-payload.sh")
    make_canopy = {"pyproject.toml": "[project]\nname='canopy'\n"}
    git(canopy, "init", "-q", "-b", "main")
    commit(canopy, {**make_canopy, "marker.txt": "canopy v1\n"}, "one")
    commit(canopy, {"marker.txt": "canopy v2\n"}, "two")

    pydm = make_repo(src / "pydm-converter-tool", {"pyproject.toml": "[project]\nname='PyDMConverter'\n"})
    sdk = make_repo(
        src / "canopy-sdk-js",
        {"package.json": "{}\n", "packages/client/package.json": '{\n  "name": "@slaclab/canopy-client"\n}\n'},
    )
    # Each runtime commit pins the SDK commit of the same generation.
    rt = src / "canopy-screen-runtime"
    rt.mkdir(parents=True)
    git(rt, "init", "-q", "-b", "main")
    rt_files = {"package.json": "{}\n", "apps/viewer/package.json": "{}\n", "packages/runtime/ir-map.json": IR_MAP}
    commit_pinning(rt, {**rt_files, "marker.txt": "canopy-screen-runtime v1\n"}, first(sdk), "one")
    commit_pinning(rt, {"marker.txt": "canopy-screen-runtime v2\n"}, head(sdk), "two")
    # So does each Trace commit: ``feature`` pins the first SDK commit, ``main`` the second.
    trace = src / "react-trace"
    trace.mkdir(parents=True)
    git(trace, "init", "-q", "-b", "main")
    trace_files = {"package.json": "{}\n", "pnpm-lock.yaml": "lock\n"}
    commit_pinning(trace, {**trace_files, "marker.txt": "react-trace v1\n"}, first(sdk), "one")
    commit_pinning(trace, {"marker.txt": "react-trace v2\n"}, head(sdk), "two")
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
        "CANOPY_VERSION", "PYDM_VERSION", "HUSKY", "SPEC",
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


def test_payload_holds_wheels_viewer_trace_and_staged(world):
    ok(build(world))
    out = world["out"]
    assert sorted(p.name for p in out.glob("*.whl")) == wheel_names(world)
    assert (out / "viewer" / "index.html").read_text().startswith("canopy-screen-runtime v2\n")
    assert json.loads((out / "viewer" / "ir-map.json").read_text()) == json.loads(IR_MAP)
    assert (out / "trace" / "index.html").read_text() == "react-trace v2\n"
    assert not (out / "widgets").exists()
    assert not (out / "spec").exists()
    assert (out / "STAGED.json").is_file()


def test_staged_names_five_commits_and_both_versions(world):
    ok(build(world))
    assert staged(world) == {
        "canopy": head(world["canopy"]),
        "runtime": head(world["rt"]),
        "sdk": head(world["sdk"]),
        "pydm": head(world["pydm"]),
        "trace": head(world["trace"]),
        "canopy_version": canopy_version(world),
        "pydm_version": pydm_version(world),
        "spec": None,
        "spec-digest": None,
        "fixtures": "absent",
    }


def test_trace_ref_is_resolved_to_its_commit(world):
    # ``feature`` pins the SDK commit the first runtime commit pins.
    ok(build(world, TRACE_REF="feature", RT_SHA=first(world["rt"])))
    assert staged(world)["trace"] == first(world["trace"])
    assert (world["out"] / "trace" / "index.html").read_text() == "react-trace v1\n"


def test_recorded_shas_are_built_instead_of_head(world):
    shas = {
        "CANOPY_SHA": first(world["canopy"]),
        "RT_SHA": first(world["rt"]),
        "SDK_SHA": first(world["sdk"]),
        "PYDM_SHA": first(world["pydm"]),
    }
    # Trace's ``feature`` pins the SDK commit the first runtime commit pins.
    # A computed version would name HEAD, not the recorded commit; given versions are used as is.
    versions = {"CANOPY_VERSION": "1.2.2", "PYDM_VERSION": "4.5.5"}
    ok(build(world, **shas, **versions, TRACE_REF="feature"))
    out = world["out"]
    s = staged(world)
    assert (s["canopy"], s["runtime"], s["sdk"], s["pydm"]) == tuple(shas.values())
    assert s["trace"] == first(world["trace"])
    assert (s["canopy_version"], s["pydm_version"]) == ("1.2.2", "4.5.5")
    assert (out / "canopy-1.2.2-py3-none-any.whl").read_text() == "canopy v1\n"
    assert (out / "pydmconverter-4.5.5-py3-none-any.whl").read_text() == "pydm-converter-tool v1\n"
    assert (out / "viewer" / "index.html").read_text() == "canopy-screen-runtime v1\ncanopy-sdk-js v1\n"


def test_short_sha_is_recorded_in_full(world):
    full = first(world["rt"])
    ok(build(world, RT_SHA=full[:10], TRACE_REF="feature"))
    assert staged(world)["runtime"] == full


# --- detached exports ---------------------------------------------------------------


def test_builds_run_only_in_exports_under_one_root(world):
    ok(build(world))
    builds = [(tool, cwd, argv) for tool, cwd, argv, _ in calls(world) if tool != "uvx"]
    assert builds
    roots = set()
    seen = set()
    for _tool, cwd, _argv in builds:
        seen.add(cwd.name)
        roots.add(cwd.parent)
    # The SDK is built only inside the runtime and Trace exports, never on its own.
    assert seen == {r.name for r in world["repos"]} - {world["sdk"].name}
    assert len(roots) == 1
    root = roots.pop()
    assert world["scratch"].resolve() in root.parents
    assert not any(src in cwd.parents or src == cwd for _, cwd, _ in builds for src in world["repos"])


def test_runtime_is_built_with_the_sdk_commit_it_pins(world):
    ok(build(world))
    # The stub's viewer build reads the SDK tree vendored into the runtime export.
    assert (world["out"] / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\ncanopy-sdk-js v2\n"
    order = [(cwd.name, argv) for tool, cwd, argv, _ in calls(world) if tool == "pnpm"]
    assert ("canopy-screen-runtime", "install --frozen-lockfile") in order
    assert not [cwd for cwd, _ in order if cwd == "canopy-sdk-js"]


def test_sdk_commit_defaults_to_the_runtime_pin_not_the_sdk_head(world):
    pinned = head(world["sdk"])
    commit(world["sdk"], {"marker.txt": "canopy-sdk-js v3\n"}, "three")
    ok(build(world))
    assert staged(world)["sdk"] == pinned
    assert (world["out"] / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\ncanopy-sdk-js v2\n"
    assert (world["out"] / "trace" / "sdk.txt").read_text() == "canopy-sdk-js v2\n"


def test_an_sdk_commit_other_than_the_runtime_pin_is_refused(world):
    proc = build(world, SDK_SHA=first(world["sdk"]))
    assert proc.returncode != 0
    assert "SDK_SHA" in proc.stderr and SDK_VENDOR in proc.stderr and head(world["sdk"]) in proc.stderr
    assert calls(world) == []


def test_a_runtime_that_does_not_vendor_the_sdk_is_refused(world):
    commit_pinning(world["rt"], {"marker.txt": "canopy-screen-runtime v3\n"}, None, "three")
    proc = build(world)
    assert proc.returncode != 0
    assert SDK_VENDOR in proc.stderr and "RT_SHA" in proc.stderr
    assert calls(world) == []


def test_a_pin_the_sdk_checkout_does_not_hold_is_refused(world):
    absent = "0123456789abcdef0123456789abcdef01234567"
    commit_pinning(world["rt"], {"marker.txt": "canopy-screen-runtime v3\n"}, absent, "three")
    proc = build(world)
    assert proc.returncode != 0
    assert absent in proc.stderr and "SDK" in proc.stderr
    assert calls(world) == []


def test_trace_is_built_with_the_image_steps(world):
    ok(build(world))
    trace = [argv for tool, cwd, argv, _ in calls(world) if tool == "pnpm" and cwd.name == "react-trace"]
    assert trace == ["install --frozen-lockfile", "exec tsc -b", "exec vite build --base=./", "run check-dist"]


def test_trace_is_built_with_the_sdk_commit_it_pins(world):
    # Matching gitlinks: the first runtime commit and ``feature`` both pin the first SDK commit.
    proc = build(world, RT_SHA=first(world["rt"]), TRACE_REF="feature")
    ok(proc)
    out = world["out"]
    # The stub's Trace build reads the SDK tree vendored into the Trace export.
    assert (out / "trace" / "sdk.txt").read_text() == "canopy-sdk-js v1\n"
    assert (out / "viewer" / "index.html").read_text() == "canopy-screen-runtime v1\ncanopy-sdk-js v1\n"
    assert staged(world)["sdk"] == first(world["sdk"])  # still one SDK commit


def test_a_trace_pinning_another_sdk_commit_is_refused(world):
    # ``feature`` pins the first SDK commit, the runtime's HEAD the second.
    proc = build(world, TRACE_REF="feature")
    assert proc.returncode != 0
    assert first(world["sdk"]) in proc.stderr and head(world["sdk"]) in proc.stderr
    assert "TRACE_REF" in proc.stderr and "RT_SHA" in proc.stderr and SDK_VENDOR in proc.stderr
    assert calls(world) == []
    assert not world["out"].exists()


def test_a_trace_commit_from_before_vendoring_is_refused(world):
    old = commit_pinning(world["trace"], {"marker.txt": "react-trace v3\n"}, None, "unvendored")
    proc = build(world)
    assert proc.returncode != 0
    assert "predates the vendored SDK" in proc.stderr
    assert old in proc.stderr and SDK_VENDOR in proc.stderr
    assert calls(world) == []
    assert not world["out"].exists()


def test_no_trace_ignores_a_trace_without_the_sdk_gitlink(world):
    commit_pinning(world["trace"], {"marker.txt": "react-trace v3\n"}, None, "unvendored")
    ok(build(world, "--no-trace"))
    assert staged(world)["trace"] is None
    assert staged(world)["sdk"] == head(world["sdk"])


def test_pnpm_never_sees_trace_and_husky_is_off(world):
    ok(build(world))
    notes = {note for tool, _, _, note in calls(world) if tool == "pnpm"}
    assert notes == {"TRACE=unset HUSKY=0"}


def test_uncommitted_edits_are_absent_from_the_payload(world):
    for repo in world["repos"]:
        (repo / "marker.txt").write_text(f"{repo.name} EDITED\n")  # tracked, modified
        (repo / "untracked.txt").write_text("x\n")
    (world["rt"] / "packages" / "runtime" / "ir-map.json").write_text('{"EDITED": {"displayName": "E"}}\n')
    git(world["sdk"], "add", "-A")  # staged, not committed

    ok(build(world))
    out = world["out"]
    texts = [p.read_text() for p in out.rglob("*") if p.is_file()]
    assert not any("EDITED" in t for t in texts)
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
    assert names == wheel_names(world)
    first_build = next(i for i, c in enumerate(calls(world)) if c[0] != "uvx")
    assert all(c[0] != "uvx" for c in calls(world)[first_build:])  # versions before any export is built


def test_given_versions_are_used_as_they_are(world):
    proc = build(world, CANOPY_VERSION="7.0.0", PYDM_VERSION="8.0.0")
    ok(proc)
    assert not [c for c in calls(world) if c[0] == "uvx"]
    assert "canopy version 7.0.0 (given by CANOPY_VERSION" in proc.stderr
    assert "PyDMConverter version 8.0.0 (given by PYDM_VERSION" in proc.stderr
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
    # No SDK build of its own: the runtime builds its vendored copy.
    assert not [c for c in calls(world) if c[0] == "pnpm" and c[1].name == "canopy-sdk-js"]


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


def test_a_trace_dist_failing_check_dist_leaves_no_payload(world):
    proc = build(world, STUB_FAIL_CHECK_DIST="1")
    assert proc.returncode != 0
    assert "failed check-dist" in proc.stderr
    assert not (world["out"] / "STAGED.json").exists()
    assert not world["out"].exists() or list(world["out"].iterdir()) == []


def test_refuses_scm_version_not_matching_staged_sha(world):
    recorded = first(world["canopy"])
    proc = build(world, CANOPY_SHA=recorded)
    assert proc.returncode != 0
    assert f"+g{head(world['canopy'])[:7]}" in proc.stderr
    assert recorded in proc.stderr
    assert "CANOPY_VERSION" in proc.stderr
    assert [c[0] for c in calls(world)] == ["uvx"]  # refused before any export is built
    assert not world["out"].exists()


def test_computed_version_without_node_is_refused(world):
    proc = build(world, STUB_SCM_VERSION="1.2.3")
    assert proc.returncode != 0
    assert "has no +g" in proc.stderr
    assert not world["out"].exists()


def test_computed_version_with_a_dirty_suffix_is_accepted(world):
    version = f"1.2.3.dev4+g{head(world['canopy'])[:7]}.d20261004"
    ok(build(world, STUB_SCM_VERSION=version, PYDM_VERSION="4.5.6"))
    assert staged(world)["canopy_version"] == version


def test_checkouts_sharing_a_directory_name_are_refused(world, tmp_path):
    twin = tmp_path / "other" / "pydm-converter-tool"
    twin.mkdir(parents=True)
    git(twin, "init", "-q", "-b", "main")
    commit_pinning(twin, {"marker.txt": "twin\n"}, head(world["sdk"]), "one")
    proc = build(world, TRACE=str(twin), TRACE_REF="main")
    assert proc.returncode != 0
    assert "pydm-converter-tool" in proc.stderr
    assert calls(world) == []


def test_a_linked_worktree_exports_under_its_repository_name(world, tmp_path):
    linked = tmp_path / "elsewhere" / "wt"
    linked.parent.mkdir()
    git(world["sdk"], "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    ok(build(world, SDK=str(linked)))
    assert (world["out"] / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\ncanopy-sdk-js v2\n"


# --- ir-map metadata ------------------------------------------------------------------


@pytest.mark.parametrize(
    "ir_map",
    [
        '{"arc": {"consumes": []}}\n',
        '{"arc": {"displayName": "Arc"}, "frame": {"displayName": ""}}\n',
        '{"arc": "not an entry"}\n',
    ],
)
def test_an_ir_map_entry_without_display_name_fails(world, ir_map):
    commit(world["rt"], {"packages/runtime/ir-map.json": ir_map}, "no metadata")
    proc = build(world)
    assert proc.returncode != 0
    assert "without displayName" in proc.stderr
    assert not world["out"].exists() or not (world["out"] / "STAGED.json").exists()


def test_a_viewer_without_ir_map_fails(world):
    git(world["rt"], "rm", "-q", "packages/runtime/ir-map.json")
    git(world["rt"], "commit", "-q", "-m", "no ir-map")
    proc = build(world)
    assert proc.returncode != 0
    assert not (world["out"] / "STAGED.json").exists()


# --- spec -----------------------------------------------------------------------------


def test_spec_is_copied_and_recorded(world, tmp_path):
    spec = make_spec(tmp_path / "spec-src")
    ok(build(world, SPEC=str(spec)))
    out = world["out"]
    assert (out / "spec" / "SPEC-SOURCE").read_text() == (spec / "SPEC-SOURCE").read_text()
    assert (out / "spec" / "ir" / "screen-ir-1.1.schema.json").read_text() == "{}\n"
    assert (out / "spec" / "widgets" / "arc.props.schema.json").is_file()
    s = staged(world)
    assert s["spec"] == SPEC_SHA
    assert s["spec-digest"].startswith("sha256:") and len(s["spec-digest"]) == len("sha256:") + 64


def test_spec_digest_is_stable_and_follows_the_tree(world, tmp_path):
    spec = make_spec(tmp_path / "spec-src")
    ok(build(world, SPEC=str(spec)))
    one = staged(world)["spec-digest"]
    again = world["out"].parent / "again"
    ok(build(world, out=again, SPEC=str(spec)))
    assert json.loads((again / "STAGED.json").read_text())["spec-digest"] == one
    # Same SPEC-SOURCE, other bytes: the stager must see a new payload.
    (spec / "ir" / "screen-ir-1.1.schema.json").write_text('{"type": "object"}\n')
    changed = world["out"].parent / "changed"
    ok(build(world, out=changed, SPEC=str(spec)))
    s = json.loads((changed / "STAGED.json").read_text())
    assert s["spec"] == SPEC_SHA
    assert s["spec-digest"] != one


@pytest.mark.parametrize("problem", ["absent", "no-source", "no-sha"])
def test_a_bad_spec_fails_before_any_tool_runs(world, tmp_path, problem):
    spec = tmp_path / "spec-src"
    if problem != "absent":
        make_spec(spec)
        if problem == "no-source":
            (spec / "SPEC-SOURCE").unlink()
        else:
            (spec / "SPEC-SOURCE").write_text("repo: slaclab/canopy-spec\nsha: abc123\n")
    proc = build(world, SPEC=str(spec))
    assert proc.returncode != 0
    assert "SPEC=" in proc.stderr
    assert calls(world) == []
    assert not world["out"].exists()


# --- the compared collections fixture set -----------------------------------------

FIX_CANOPY = "tests/canopy_mcp/fixtures/collections"
FIX_RT = "apps/viewer/e2e/fixtures/collections"
# Bytes that are not UTF-8, as invalid/not-utf8.json is: the comparison is by bytes.
NOT_UTF8 = b'{"name": "\xff\xfe"}\n'
FIXTURE_SET = {
    "member-key-vectors.json": b'{"vectors": []}\n',
    "valid/facility/injector.json": b'{"name": "Injector"}\n',
    "valid/personal/grid.json": b'{"layout": {"split": "row"}}\n',
    "invalid/not-utf8.json": NOT_UTF8,
    "invalid/not-a-file.json/keep": b"",
    "responses/state.json": b'{"collections": []}\n',
}


def commit_bytes(repo: Path, files: dict[str, bytes | None], message: str) -> str:
    """Commit raw bytes (None deletes); a pinned SDK commit carries over, as in commit()."""
    pin = sdk_pin(repo)
    for name, data in files.items():
        f = repo / name
        if data is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    return commit_pinning(repo, {}, pin, message)


def fixtures(prefix: str, files: dict[str, bytes | None]) -> dict[str, bytes | None]:
    return {f"{prefix}/{name}": data for name, data in files.items()}


def with_fixtures(world: dict, canopy: dict | None = None, rt: dict | None = None) -> None:
    """Commit the fixture set to both repos' HEAD; ``canopy``/``rt`` override single files."""
    commit_bytes(world["canopy"], fixtures(FIX_CANOPY, {**FIXTURE_SET, **(canopy or {})}), "fixtures")
    commit_bytes(world["rt"], fixtures(FIX_RT, {**FIXTURE_SET, **(rt or {})}), "fixtures")


def refused(world: dict, proc, path: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"collections fixture {path} " in proc.stderr
    assert not world["out"].exists() or not (world["out"] / "STAGED.json").exists()
    # The comparison runs before any tool: a refusal builds nothing.
    assert calls(world) == []


def test_runtime_without_the_fixture_dir_records_absent(world):
    # Canopy carries fixtures the runtime commit predates: nothing to compare.
    commit_bytes(world["canopy"], fixtures(FIX_CANOPY, FIXTURE_SET), "fixtures")
    proc = build(world)
    ok(proc)
    assert staged(world)["fixtures"] == "absent"
    assert "fixtures absent" in proc.stderr


def test_identical_fixture_sets_record_compared(world):
    with_fixtures(world)
    proc = build(world)
    ok(proc)
    assert staged(world)["fixtures"] == "compared"
    assert "fixtures compared" in proc.stderr


def test_a_byte_difference_is_refused_naming_the_path(world):
    with_fixtures(world, rt={"valid/personal/grid.json": b'{"layout": {"split": "column"}}\n'})
    proc = build(world)
    refused(world, proc, "valid/personal/grid.json")
    assert "differs between" in proc.stderr


def test_a_non_utf8_byte_difference_is_refused(world):
    with_fixtures(world, canopy={"invalid/not-utf8.json": NOT_UTF8.replace(b"\xfe", b"\xfd")})
    refused(world, build(world), "invalid/not-utf8.json")


def test_the_first_differing_path_is_named(world):
    with_fixtures(
        world,
        rt={"responses/state.json": b"{}\n", "invalid/not-utf8.json": b"{}\n", "valid/personal/grid.json": b"{}\n"},
    )
    refused(world, build(world), "invalid/not-utf8.json")


def test_a_fixture_only_in_the_runtime_is_refused(world):
    with_fixtures(world, rt={"responses/extra.json": b"{}\n"})
    proc = build(world)
    refused(world, proc, "responses/extra.json")
    assert "is in RT_SHA=" in proc.stderr


def test_a_fixture_only_in_canopy_is_refused(world):
    with_fixtures(world, canopy={"invalid/extra.json": b"{}\n"})
    proc = build(world)
    refused(world, proc, "invalid/extra.json")
    assert "is in CANOPY_SHA=" in proc.stderr


def test_a_runtime_dir_without_canopy_fixtures_is_refused(world):
    commit_bytes(world["rt"], fixtures(FIX_RT, FIXTURE_SET), "fixtures")
    refused(world, build(world), "invalid/not-a-file.json/keep")


def test_a_fixture_deleted_on_one_side_is_refused(world):
    with_fixtures(world)
    commit_bytes(world["rt"], fixtures(FIX_RT, {"member-key-vectors.json": None}), "drop")
    refused(world, build(world), "member-key-vectors.json")


def test_library_and_other_paths_are_not_compared(world):
    with_fixtures(
        world,
        canopy={"library/als.json": b'{"canopy": 1}\n', "README.md": b"canopy\n"},
        rt={"library/als.json": b'{"runtime": 2}\n', "notes.txt": b"rt\n"},
    )
    ok(build(world))
    assert staged(world)["fixtures"] == "compared"


def test_the_recorded_commits_are_compared_not_head(world):
    with_fixtures(world)
    matching = head(world["canopy"])
    # Canopy's HEAD drifts from the runtime; the recorded CANOPY_SHA does not.
    commit_bytes(world["canopy"], fixtures(FIX_CANOPY, {"responses/state.json": b"{}\n"}), "drift")
    ok(build(world, CANOPY_SHA=matching, CANOPY_VERSION="1.2.2"))
    assert staged(world)["fixtures"] == "compared"
    assert staged(world)["canopy"] == matching


def test_a_drifted_recorded_commit_is_refused_even_when_head_matches(world):
    with_fixtures(world)
    commit_bytes(world["rt"], fixtures(FIX_RT, {"responses/state.json": b"{}\n"}), "drift")
    drifted = head(world["rt"])
    commit_bytes(world["rt"], fixtures(FIX_RT, {"responses/state.json": FIXTURE_SET["responses/state.json"]}), "back")
    refused(world, build(world, RT_SHA=drifted), "responses/state.json")


def test_uncommitted_fixture_edits_are_not_compared(world):
    with_fixtures(world)
    (world["rt"] / FIX_RT / "valid" / "personal" / "grid.json").write_bytes(b"edited\n")
    (world["canopy"] / FIX_CANOPY / "responses" / "new.json").write_bytes(b"{}\n")
    ok(build(world))
    assert staged(world)["fixtures"] == "compared"


def test_help_names_the_fixture_check(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "fixtures" in proc.stdout
    assert FIX_RT in proc.stdout
