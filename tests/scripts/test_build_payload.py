"""scripts/build-payload.sh against throwaway checkouts.

Five fake repositories stand in for canopy, canopy-screen-runtime,
canopy-sdk-js, pydm-converter-tool and react-trace. ``pnpm``, ``uv`` and
``uvx`` are PATH stubs that log every call (tool, working directory, argv) and
write the artifact the real tool would, built from the files they find in
their working directory, so the payload's content shows which tree was built.
The runtime and Trace each commit a ``pnpm-lock.yaml`` resolving the SDK's packages.
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
SDK_VERSION = "0.3.0"
INTEGRITY = {"client": "sha512-client==", "react": "sha512-react==", "shell": "sha512-shell=="}


def lockfile(entries: dict[str, tuple[str, str]]) -> str:
    """A pnpm lockfile whose packages section holds ``{package: (version, integrity)}``.

    Laid out as a real one: the importers and snapshots sections name the same
    packages, and an entry may carry fields after its resolution.
    """
    lines = ["lockfileVersion: '9.0'", "", "importers:", "", "  .:", "    dependencies:"]
    for package, (version, _) in entries.items():
        lines += [
            f"      '@slaclab/canopy-{package}':",
            f"        specifier: ^{version}",
            f"        version: {version}",
        ]
    lines += [
        "",
        "packages:",
        "",
        "  '@rolldown/pluginutils@1.0.1':",
        "    resolution: {integrity: sha512-other==}",
        "",
    ]
    for package, (version, integrity) in entries.items():
        lines += [
            f"  '@slaclab/canopy-{package}@{version}':",
            f"    resolution: {{integrity: {integrity}}}",
            "    peerDependencies:",
            "      react: ^19.0.0",
            "",
        ]
    lines += ["snapshots:", ""]
    for package, (version, _) in entries.items():
        lines += [
            f"  '@slaclab/canopy-{package}@{version}(react@19.2.7)':",
            "    dependencies:",
            "      react: 19.2.7",
            "",
        ]
    return "\n".join(lines) + "\n"


def sdk_lock(*packages: str, **changed: tuple[str, str]) -> str:
    """A lockfile resolving ``packages`` to the release; ``changed`` overrides single entries."""
    return lockfile({p: changed.get(p, (SDK_VERSION, INTEGRITY[p])) for p in packages})


# The runtime also resolves canopy-react, which Trace does not use.
RT_LOCK = sdk_lock("client", "react", "shell")
TRACE_LOCK = sdk_lock("client", "shell")

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
    "install --frozen-lockfile")
        case "${STUB_FAIL_INSTALL:-}" in "$(basename "$PWD")") exit 1 ;; esac
        ;;
    "run build")
        [ -d apps/viewer ] || exit 97
        [ -z "${STUB_FAIL_VIEWER:-}" ] || exit 1
        mkdir -p apps/viewer/dist
        cat marker.txt > apps/viewer/dist/index.html
        cp packages/runtime/ir-map.json apps/viewer/dist/ir-map.json
        ;;
    "exec tsc -b") ;;
    "exec vite build --base=./")
        mkdir -p dist
        cat marker.txt > dist/index.html
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


def canopy_version(world: dict) -> str:
    """The version the uvx stub computes for the canopy checkout's HEAD."""
    return f"1.2.3.dev4+g{head(world['canopy'])[:7]}"


def pydm_version(world: dict) -> str:
    return f"4.5.6+g{head(world['pydm'])[:7]}"


# The runtime's ir-map.json carries the editor metadata the catalog is built from.
IR_MAP = '{"arc": {"consumes": [], "verbatim": true, "displayName": "Arc"}}\n'
SPEC_SHA = "0123456789abcdef0123456789abcdef01234567"


def make_spec(root: Path, schema: str = "{}\n") -> Path:
    """A staged spec directory: ``SPEC-SOURCE``, ``ir/`` and ``widgets/``."""
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
        {
            "package.json": "{}\n",
            **{
                f"packages/{package}/package.json": json.dumps(
                    {"name": f"@slaclab/canopy-{package}", "version": SDK_VERSION}
                )
                for package in INTEGRITY
            },
        },
    )
    rt_files = {
        "package.json": "{}\n",
        "pnpm-lock.yaml": RT_LOCK,
        "apps/viewer/package.json": "{}\n",
        "packages/runtime/ir-map.json": IR_MAP,
    }
    rt = make_repo(src / "canopy-screen-runtime", rt_files)
    trace = make_repo(src / "react-trace", {"package.json": "{}\n", "pnpm-lock.yaml": TRACE_LOCK})
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
        "PYDM", "RT", "SDK", "SDK_SHA", "TRACE", "TRACE_REF", "CANOPY_SHA", "RT_SHA", "PYDM_SHA",
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
    # SDK_SHA has no default in the script; here it is the SDK's HEAD unless a test says otherwise.
    env = {k: v for k, v in {**world["env"], "SDK_SHA": head(world["sdk"])}.items() if k not in unset}
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


def resolved_entry(package: str) -> dict:
    return {"version": SDK_VERSION, "integrity": INTEGRITY[package]}


def staged(world: dict) -> dict:
    return json.loads((world["out"] / "STAGED.json").read_text())


# --- the payload ---------------------------------------------------------------


def test_payload_holds_wheels_viewer_trace_and_staged(world):
    ok(build(world))
    out = world["out"]
    assert sorted(p.name for p in out.glob("*.whl")) == wheel_names(world)
    assert (out / "viewer" / "index.html").read_text() == "canopy-screen-runtime v2\n"
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
        "sdk_packages": {
            "runtime": {f"@slaclab/canopy-{p}": resolved_entry(p) for p in ("client", "react", "shell")},
            "trace": {f"@slaclab/canopy-{p}": resolved_entry(p) for p in ("client", "shell")},
        },
        "pydm": head(world["pydm"]),
        "trace": head(world["trace"]),
        "canopy_version": canopy_version(world),
        "pydm_version": pydm_version(world),
        "spec": None,
        "spec-digest": None,
        "fixtures": "absent",
        "workspace": "absent",
        "vectors": "absent",
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
    assert (out / "viewer" / "index.html").read_text() == "canopy-screen-runtime v1\n"


def test_short_sha_is_recorded_in_full(world):
    full = first(world["rt"])
    ok(build(world, RT_SHA=full[:10]))
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
    # The SDK comes from the registry: it is never built.
    assert seen == {r.name for r in world["repos"]} - {world["sdk"].name}
    assert len(roots) == 1
    root = roots.pop()
    assert world["scratch"].resolve() in root.parents
    assert not any(src in cwd.parents or src == cwd for _, cwd, _ in builds for src in world["repos"])


def test_the_runtime_installs_its_lockfile_then_builds(world):
    ok(build(world))
    runtime = [argv for tool, cwd, argv, _ in calls(world) if tool == "pnpm" and cwd.name == "canopy-screen-runtime"]
    assert runtime == ["install --frozen-lockfile", "run build"]


def test_trace_is_built_with_its_own_build_steps(world):
    ok(build(world))
    trace = [argv for tool, cwd, argv, _ in calls(world) if tool == "pnpm" and cwd.name == "react-trace"]
    assert trace == ["install --frozen-lockfile", "exec tsc -b", "exec vite build --base=./", "run check-dist"]


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
    assert not [c for c in calls(world) if c[0] == "pnpm" and c[1].name == "canopy-sdk-js"]


# --- refusals ----------------------------------------------------------------------


@pytest.mark.parametrize("var", ["PYDM", "RT", "SDK", "SDK_SHA", "TRACE", "TRACE_REF"])
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
    proc = build(world, "--help", out=False, unset=("PYDM", "RT", "SDK", "SDK_SHA", "TRACE", "TRACE_REF"))
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
    commit(twin, {"marker.txt": "twin\n"}, "one")
    proc = build(world, TRACE=str(twin), TRACE_REF="main")
    assert proc.returncode != 0
    assert "pydm-converter-tool" in proc.stderr
    assert calls(world) == []


def test_a_linked_worktree_exports_under_its_repository_name(world, tmp_path):
    linked = tmp_path / "elsewhere" / "wt"
    linked.parent.mkdir()
    git(world["sdk"], "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    ok(build(world, SDK=str(linked)))
    assert staged(world)["sdk"] == head(world["sdk"])


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
    "match-vectors.json": b'{"about": []}\n',
    "valid/facility/injector.json": b'{"name": "Injector"}\n',
    "valid/personal/grid.json": b'{"layout": {"split": "row"}}\n',
    "invalid/not-utf8.json": NOT_UTF8,
    "invalid/not-a-file.json/keep": b"",
    "responses/state.json": b'{"collections": []}\n',
}


def commit_bytes(repo: Path, files: dict[str, bytes | None], message: str) -> str:
    """Commit raw bytes (None deletes)."""
    for name, data in files.items():
        f = repo / name
        if data is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    return commit(repo, {}, message)


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


def test_match_vectors_dropped_by_the_runtime_is_refused(world):
    with_fixtures(world)
    commit_bytes(world["rt"], fixtures(FIX_RT, {"match-vectors.json": None}), "drop")
    proc = build(world)
    refused(world, proc, "match-vectors.json")
    assert "is in CANOPY_SHA=" in proc.stderr


def test_library_and_other_paths_are_not_compared(world):
    with_fixtures(
        world,
        canopy={"library/site.json": b'{"canopy": 1}\n', "README.md": b"canopy\n"},
        rt={"library/site.json": b'{"runtime": 2}\n', "notes.txt": b"rt\n"},
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


# --- the compared workspace fixture set ---------------------------------------------

WS_CANOPY = "tests/canopy_mcp/fixtures/workspace"
WS_RT = "apps/viewer/e2e/fixtures/workspace"
WORKSPACE_SET = {
    "commands/open.json": b'{"args": {"ref": "string"}}\n',
    "rejections/tab_limit.json": b'{"cases": []}\n',
    "routes/claim.json": b'{"route": "claim"}\n',
    "scenarios/expiry.json": b'{"steps": []}\n',
    "context.json": b'{"context": {}}\n',
    "state.sample.json": b'{"tabs": []}\n',
    "README.md": b"workspace\n",
    "bytes.bin": NOT_UTF8,
}


def with_workspace(world: dict, canopy: dict | None = None, rt: dict | None = None) -> None:
    """Commit the workspace set to both repos' HEAD; ``canopy``/``rt`` override single files."""
    commit_bytes(world["canopy"], fixtures(WS_CANOPY, {**WORKSPACE_SET, **(canopy or {})}), "workspace")
    commit_bytes(world["rt"], fixtures(WS_RT, {**WORKSPACE_SET, **(rt or {})}), "workspace")


def workspace_refused(world: dict, proc, path: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"workspace fixture {path} " in proc.stderr
    assert not world["out"].exists() or not (world["out"] / "STAGED.json").exists()
    assert calls(world) == []


def test_build_payload_workspace_absent_when_neither_has_it(world):
    proc = build(world)
    ok(proc)
    assert staged(world)["workspace"] == "absent"
    assert "workspace absent" in proc.stderr


def test_build_payload_workspace_equal_copies_record_compared(world):
    with_workspace(world)
    proc = build(world)
    ok(proc)
    assert staged(world)["workspace"] == "compared"
    assert "workspace compared" in proc.stderr
    # The collections check is independent of the workspace one.
    assert staged(world)["fixtures"] == "absent"


def test_build_payload_workspace_and_collections_both_compared(world):
    with_fixtures(world)
    with_workspace(world)
    ok(build(world))
    assert staged(world)["fixtures"] == "compared"
    assert staged(world)["workspace"] == "compared"


def test_build_payload_workspace_byte_difference_is_refused(world):
    with_workspace(world, rt={"scenarios/expiry.json": b'{"steps": [1]}\n'})
    proc = build(world)
    workspace_refused(world, proc, "scenarios/expiry.json")
    assert "differs between" in proc.stderr
    assert f"({WS_CANOPY}/ vs {WS_RT}/)" in proc.stderr


def test_build_payload_workspace_non_utf8_difference_is_refused(world):
    with_workspace(world, canopy={"bytes.bin": NOT_UTF8.replace(b"\xfe", b"\xfd")})
    workspace_refused(world, build(world), "bytes.bin")


def test_build_payload_workspace_every_file_is_compared(world):
    # Unlike the collections set, no path below the directory is exempt.
    with_workspace(world, rt={"README.md": b"runtime\n"})
    workspace_refused(world, build(world), "README.md")


def test_build_payload_workspace_first_differing_path_is_named(world):
    with_workspace(world, rt={"state.sample.json": b"{}\n", "context.json": b"{}\n", "routes/claim.json": b"{}\n"})
    workspace_refused(world, build(world), "context.json")


def test_build_payload_workspace_only_in_canopy_is_refused(world):
    commit_bytes(world["canopy"], fixtures(WS_CANOPY, WORKSPACE_SET), "workspace")
    proc = build(world)
    workspace_refused(world, proc, "README.md")
    assert "is in CANOPY_SHA=" in proc.stderr


def test_build_payload_workspace_only_in_the_runtime_is_refused(world):
    commit_bytes(world["rt"], fixtures(WS_RT, WORKSPACE_SET), "workspace")
    proc = build(world)
    workspace_refused(world, proc, "README.md")
    assert "is in RT_SHA=" in proc.stderr


def test_build_payload_workspace_extra_file_on_one_side_is_refused(world):
    with_workspace(world, canopy={"commands/close.json": b"{}\n"})
    proc = build(world)
    workspace_refused(world, proc, "commands/close.json")
    assert "is in CANOPY_SHA=" in proc.stderr


def test_build_payload_workspace_file_deleted_on_one_side_is_refused(world):
    with_workspace(world)
    commit_bytes(world["rt"], fixtures(WS_RT, {"routes/claim.json": None}), "drop")
    proc = build(world)
    workspace_refused(world, proc, "routes/claim.json")
    assert "is in CANOPY_SHA=" in proc.stderr


def test_build_payload_workspace_recorded_commits_are_compared_not_head(world):
    with_workspace(world)
    matching = head(world["rt"])
    commit_bytes(world["rt"], fixtures(WS_RT, {"context.json": b"{}\n"}), "drift")
    ok(build(world, RT_SHA=matching))
    assert staged(world)["workspace"] == "compared"
    assert staged(world)["runtime"] == matching


def test_build_payload_workspace_recorded_canopy_commit_without_it_is_refused(world):
    # The runtime HEAD carries the copy; the recorded canopy commit predates it.
    before = head(world["canopy"])
    with_workspace(world)
    proc = build(world, CANOPY_SHA=before, CANOPY_VERSION="1.2.2")
    workspace_refused(world, proc, "README.md")
    assert "is in RT_SHA=" in proc.stderr


def test_build_payload_workspace_uncommitted_edits_are_not_compared(world):
    with_workspace(world)
    (world["rt"] / WS_RT / "context.json").write_bytes(b"edited\n")
    (world["canopy"] / WS_CANOPY / "commands" / "new.json").write_bytes(b"{}\n")
    ok(build(world))
    assert staged(world)["workspace"] == "compared"


def test_build_payload_help_names_the_workspace_check(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "workspace" in proc.stdout
    assert WS_CANOPY in proc.stdout
    assert WS_RT in proc.stdout


# --- the compared vector files ---------------------------------------------------

VEC_CANOPY = "src/canopy/contracts"
VEC_SDK = "packages/client/test-vectors"
VECTORS = {
    "heartbeat_vectors.json": b'{"contract": "machine statement"}\n',
    # Not UTF-8 on purpose: the comparison is by bytes.
    "write_outcome_vectors.json": b'{"vectors": [{"outcome": "written", "note": "\xff"}]}\n',
}


def with_vectors(world: dict, canopy: dict | None = None, sdk: dict | None = None) -> None:
    """Commit the vector files to canopy's HEAD and the SDK's.

    ``canopy``/``sdk`` override single files (None leaves the file out of that side).
    """
    for repo, prefix, override in ((world["canopy"], VEC_CANOPY, canopy), (world["sdk"], VEC_SDK, sdk)):
        files = {k: v for k, v in {**VECTORS, **(override or {})}.items() if v is not None}
        if files:
            commit_bytes(repo, fixtures(prefix, files), "vectors")


def vector_refused(world: dict, proc, name: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"vector file {name} " in proc.stderr
    assert not world["out"].exists() or not (world["out"] / "STAGED.json").exists()
    # The comparison runs before any tool: a refusal builds nothing.
    assert calls(world) == []


def test_without_vector_files_on_either_side_records_absent(world):
    proc = build(world)
    ok(proc)
    assert staged(world)["vectors"] == "absent"
    assert "vectors absent" in proc.stderr


def test_identical_vector_files_record_compared(world):
    with_vectors(world)
    proc = build(world)
    ok(proc)
    assert staged(world)["vectors"] == "compared"
    assert "vectors compared" in proc.stderr


def test_one_byte_changed_in_the_sdk_write_outcome_copy_is_refused_naming_the_file(world):
    with_vectors(
        world, sdk={"write_outcome_vectors.json": VECTORS["write_outcome_vectors.json"].replace(b"written", b"writtem")}
    )
    proc = build(world)
    vector_refused(world, proc, "write_outcome_vectors.json")
    assert "differs between CANOPY_SHA=" in proc.stderr
    assert f"SDK_SHA={head(world['sdk'])}" in proc.stderr


def test_a_non_utf8_byte_changed_in_the_sdk_copy_is_refused(world):
    with_vectors(
        world, sdk={"write_outcome_vectors.json": VECTORS["write_outcome_vectors.json"].replace(b"\xff", b"\xfe")}
    )
    vector_refused(world, build(world), "write_outcome_vectors.json")


def test_a_vector_file_missing_from_the_sdk_is_refused(world):
    with_vectors(world, sdk={"write_outcome_vectors.json": None})
    proc = build(world)
    vector_refused(world, proc, "write_outcome_vectors.json")
    assert "is in CANOPY_SHA=" in proc.stderr
    assert "but not in SDK_SHA=" in proc.stderr


def test_a_vector_file_missing_from_canopy_is_refused(world):
    with_vectors(world, canopy={"write_outcome_vectors.json": None})
    proc = build(world)
    vector_refused(world, proc, "write_outcome_vectors.json")
    assert "is in SDK_SHA=" in proc.stderr


def test_a_heartbeat_vector_file_missing_from_the_sdk_is_refused(world):
    with_vectors(world, sdk={"heartbeat_vectors.json": None})
    proc = build(world)
    vector_refused(world, proc, "heartbeat_vectors.json")
    assert "but not in SDK_SHA=" in proc.stderr


def test_canopy_vectors_against_an_sdk_commit_without_them_are_refused(world):
    # Canopy carries the vectors; the SDK commit predates them.
    commit_bytes(world["canopy"], fixtures(VEC_CANOPY, VECTORS), "vectors")
    # C-sorted: the first file missing from the SDK is the one named.
    vector_refused(world, build(world), "heartbeat_vectors.json")


def test_the_recorded_sdk_commit_is_compared_not_sdk_head(world):
    with_vectors(world)
    matching = head(world["sdk"])
    commit_bytes(world["sdk"], fixtures(VEC_SDK, {"write_outcome_vectors.json": b"{}\n"}), "drift")
    ok(build(world, SDK_SHA=matching))
    assert staged(world)["vectors"] == "compared"


def test_the_recorded_canopy_commit_is_compared_not_head(world):
    with_vectors(world)
    matching = head(world["canopy"])
    commit_bytes(world["canopy"], fixtures(VEC_CANOPY, {"write_outcome_vectors.json": b"{}\n"}), "drift")
    ok(build(world, CANOPY_SHA=matching, CANOPY_VERSION="1.2.2"))
    assert staged(world)["vectors"] == "compared"


def test_uncommitted_vector_edits_are_not_compared(world):
    with_vectors(world)
    (world["sdk"] / VEC_SDK / "write_outcome_vectors.json").write_bytes(b"edited\n")
    (world["canopy"] / VEC_CANOPY / "write_outcome_vectors.json").write_bytes(b"edited\n")
    ok(build(world))
    assert staged(world)["vectors"] == "compared"


def test_help_names_the_vector_check(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "vectors" in proc.stdout
    assert VEC_SDK in proc.stdout
    assert "write_outcome_vectors.json" in proc.stdout


def test_workspace_and_vector_checks_are_recorded_side_by_side(world):
    with_workspace(world)
    with_vectors(world)
    ok(build(world))
    assert staged(world)["workspace"] == "compared"
    assert staged(world)["vectors"] == "compared"


# --- one SDK per payload ---------------------------------------------------------


def built_nothing_after_the_installs(world: dict) -> None:
    pnpm = [argv for tool, _, argv, _ in calls(world) if tool == "pnpm"]
    assert set(pnpm) <= {"install --frozen-lockfile"}
    assert not (world["out"] / "STAGED.json").exists()


def test_the_sdk_commit_has_no_default(world):
    proc = build(world, unset=("SDK_SHA",))
    assert proc.returncode != 0
    assert "set SDK_SHA" in proc.stderr
    assert calls(world) == []


def test_the_recorded_sdk_commit_is_not_the_sdk_head(world):
    ok(build(world, SDK_SHA=first(world["sdk"])))
    assert staged(world)["sdk"] == first(world["sdk"])


def test_shared_fixtures_are_checked_without_any_gitlink(world):
    # The clean tops have no submodule: the comparison must still be reached.
    with_fixtures(world)
    with_workspace(world)
    with_vectors(world)
    for repo in (world["rt"], world["trace"]):
        assert "160000" not in git(repo, "ls-tree", "-r", "HEAD")
    ok(build(world))
    s = staged(world)
    assert (s["fixtures"], s["workspace"], s["vectors"]) == ("compared", "compared", "compared")


def test_another_sdk_version_in_trace_is_refused(world):
    commit(world["trace"], {"pnpm-lock.yaml": sdk_lock("client", "shell", client=("0.2.0", "sha512-old=="))}, "old")
    proc = build(world)
    assert proc.returncode != 0
    assert "@slaclab/canopy-client" in proc.stderr and "0.2.0" in proc.stderr and SDK_VERSION in proc.stderr
    assert "RT_SHA=" in proc.stderr and "TRACE_SHA=" in proc.stderr and "one SDK" in proc.stderr
    built_nothing_after_the_installs(world)


def test_the_same_version_with_another_integrity_is_refused(world):
    # A rebuilt pack under one version number: not the same SDK.
    commit(
        world["rt"],
        {"pnpm-lock.yaml": sdk_lock("client", "react", "shell", shell=(SDK_VERSION, "sha512-rebuilt=="))},
        "pack",
    )
    proc = build(world)
    assert proc.returncode != 0
    assert "@slaclab/canopy-shell" in proc.stderr and "sha512-rebuilt==" in proc.stderr
    assert INTEGRITY["shell"] in proc.stderr
    built_nothing_after_the_installs(world)


def test_a_package_on_one_side_only_is_not_compared_but_is_recorded(world):
    ok(build(world))
    packages = staged(world)["sdk_packages"]
    assert "@slaclab/canopy-react" in packages["runtime"]
    assert "@slaclab/canopy-react" not in packages["trace"]


def test_no_trace_records_only_the_runtime_and_compares_nothing(world):
    commit(world["trace"], {"pnpm-lock.yaml": sdk_lock("client", "shell", client=("0.2.0", "sha512-old=="))}, "old")
    ok(build(world, "--no-trace"))
    packages = staged(world)["sdk_packages"]
    assert packages["trace"] is None
    assert set(packages["runtime"]) == {f"@slaclab/canopy-{p}" for p in ("client", "react", "shell")}


@pytest.mark.parametrize("repo,label", [("rt", "runtime"), ("trace", "Trace")])
def test_a_lockfile_without_the_sdk_is_refused(world, repo, label):
    commit(world[repo], {"pnpm-lock.yaml": lockfile({})}, "no sdk")
    proc = build(world)
    assert proc.returncode != 0
    assert f"the {label} " in proc.stderr
    assert "resolves no @slaclab/canopy-* package" in proc.stderr
    built_nothing_after_the_installs(world)


@pytest.mark.parametrize(
    "repo,name,label", [("rt", "canopy-screen-runtime", "runtime"), ("trace", "react-trace", "Trace")]
)
def test_a_failed_frozen_install_says_the_lockfile_must_resolve_the_release(world, repo, name, label):
    proc = build(world, STUB_FAIL_INSTALL=name)
    assert proc.returncode != 0
    assert f"installing the {label} export of {head(world[repo])} failed" in proc.stderr
    assert "--frozen-lockfile" in proc.stderr
    built_nothing_after_the_installs(world)


def test_only_a_frozen_install_is_ever_run(world):
    ok(build(world))
    installs = {argv for tool, _, argv, _ in calls(world) if tool == "pnpm" and argv.startswith("install")}
    assert installs == {"install --frozen-lockfile"}


def test_two_versions_in_one_lockfile_are_refused(world):
    text = RT_LOCK.replace(
        "snapshots:", "  '@slaclab/canopy-client@0.2.0':\n    resolution: {integrity: sha512-old==}\n\nsnapshots:"
    )
    commit(world["rt"], {"pnpm-lock.yaml": text}, "two")
    proc = build(world)
    assert proc.returncode != 0
    assert "more than one version" in proc.stderr and "@slaclab/canopy-client" in proc.stderr
    built_nothing_after_the_installs(world)


def test_an_entry_without_integrity_is_refused(world):
    text = RT_LOCK.replace(
        f"resolution: {{integrity: {INTEGRITY['shell']}}}", "resolution: {tarball: https://example.org/x.tgz}"
    )
    commit(world["rt"], {"pnpm-lock.yaml": text}, "no integrity")
    proc = build(world)
    assert proc.returncode != 0
    assert "no integrity for @slaclab/canopy-shell" in proc.stderr
    built_nothing_after_the_installs(world)


def test_a_local_pack_in_a_lockfile_is_refused(world):
    # The registry release is what this script builds; a file: resolution is no version SDK_SHA holds.
    pack = "file:../packs/slaclab-canopy-client-0.3.0.tgz"
    both = {"client": (pack, INTEGRITY["client"]), "shell": (SDK_VERSION, INTEGRITY["shell"])}
    commit(world["rt"], {"pnpm-lock.yaml": lockfile(both)}, "pack")
    commit(world["trace"], {"pnpm-lock.yaml": lockfile(both)}, "pack")
    proc = build(world)
    assert proc.returncode != 0
    assert f"@slaclab/canopy-client is resolved to {pack}" in proc.stderr
    built_nothing_after_the_installs(world)


def bump_sdk(world: dict, version: str) -> str:
    """An SDK commit whose client package is at ``version``."""
    return commit(
        world["sdk"],
        {"packages/client/package.json": json.dumps({"name": "@slaclab/canopy-client", "version": version})},
        version,
    )


def test_a_version_the_sdk_commit_does_not_hold_is_refused(world):
    other = bump_sdk(world, "0.4.0")
    proc = build(world)
    assert proc.returncode != 0
    assert f"SDK_SHA={other}" in proc.stderr and "holds 0.4.0" in proc.stderr
    assert "@slaclab/canopy-client is resolved to 0.3.0" in proc.stderr
    built_nothing_after_the_installs(world)


def test_the_commit_holding_the_version_passes_when_the_head_has_moved(world):
    released = head(world["sdk"])
    bump_sdk(world, "0.4.0")
    ok(build(world, SDK_SHA=released))
    assert staged(world)["sdk"] == released


def test_a_package_the_sdk_commit_does_not_have_is_refused(world):
    git(world["sdk"], "rm", "-q", "packages/react/package.json")
    git(world["sdk"], "commit", "-q", "-m", "no react")
    proc = build(world)
    assert proc.returncode != 0
    assert "@slaclab/canopy-react@0.3.0 is resolved" in proc.stderr and "no such package" in proc.stderr
    built_nothing_after_the_installs(world)


def test_a_release_tag_on_another_commit_is_refused(world):
    git(world["sdk"], "tag", f"v{SDK_VERSION}", first(world["sdk"]))
    proc = build(world)
    assert proc.returncode != 0
    assert f"tag v{SDK_VERSION} names {first(world['sdk'])}" in proc.stderr
    built_nothing_after_the_installs(world)


def test_a_release_tag_on_the_sdk_commit_passes(world):
    git(world["sdk"], "tag", f"v{SDK_VERSION}")
    proc = build(world)
    ok(proc)
    assert f"sdk tag v{SDK_VERSION} names SDK_SHA" in proc.stderr


def test_an_absent_release_tag_is_stated_not_refused(world):
    proc = build(world)
    ok(proc)
    assert f"sdk tag v{SDK_VERSION} is not in SDK" in proc.stderr


def test_help_names_the_one_sdk_check(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "sdk_packages" in proc.stdout
    assert "no default" in proc.stdout
    assert "vendor/canopy-sdk-js" not in proc.stdout
