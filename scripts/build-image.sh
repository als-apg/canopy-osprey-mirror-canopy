#!/usr/bin/env bash
# Build the CANOPY appliance image (docker/Dockerfile.appliance), canopy:dev by default.
#
# The host builds two artifacts and hands them to the Dockerfile as named build
# contexts that OVERRIDE its `pydm` and `viewer` export stages, so Docker never
# needs the runtime workspace's local `link:` to the SDK or a pydm-converter
# release on PyPI:
#   pydm   — a PyDMConverter wheel built from a `git archive` copy of $PYDM HEAD
#   viewer — the canopy-screen-runtime viewer dist ($RT/apps/viewer/dist)
# react-trace is built INSIDE Docker from a `git archive` of one resolved commit
# of $TRACE ($TRACE_REF), passed as the `trace-src` context. With --no-trace
# both `trace-src` and `trace` are empty contexts and the image ships an empty
# /app/trace.
#
# Provenance goes in as build-args and ends up as org.canopy.* labels:
# CANOPY_SHA / RUNTIME_SHA (HEAD, `+dirty` when a TRACKED file differs from it),
# TRACE_SHA and TRACE_LOCK_SHA256 (sha256 of that commit's pnpm-lock.yaml).
# scripts/verify-image.sh checks them against the checkouts afterwards.
#
# --worktree builds uncommitted work with exact provenance, for a prototype whose
# checkouts are not committed yet: Trace is then a copy of $TRACE's working tree
# (tracked and untracked, non-ignored files, .claude/ excluded; TRACE_REF must be
# TRACE's HEAD), and CANOPY_SHA / RUNTIME_SHA / TRACE_SHA are the commit when the
# tree is clean, else `<commit>+dirty.<fingerprint>` where any tracked change or
# untracked, non-ignored file makes it dirty and the fingerprint (tree_fp) is a
# content hash of the working tree. verify-image.sh recomputes the fingerprint.
#
# Needs git, tar, uv/uvx, pnpm and docker (BuildKit). Run with --help for inputs.
set -euo pipefail

PROG=build-image
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$ROOT/docker/Dockerfile.appliance"

usage() {
    cat <<EOF
Usage: PYDM=<dir> RT=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-image.sh [--worktree] [--dry-run]
       PYDM=<dir> RT=<dir> scripts/build-image.sh --no-trace [--dry-run]

Builds the CANOPY appliance image from this canopy checkout ($ROOT).

Required inputs (environment):
  PYDM        pydm-converter-tool checkout (no tracked changes; HEAD is built)
  RT          canopy-screen-runtime checkout (pnpm workspace with apps/viewer)
  TRACE       react-trace checkout (required unless --no-trace)
  TRACE_REF   git ref in TRACE to build, e.g. a branch (required unless --no-trace);
              it must resolve to a commit that contains pnpm-lock.yaml

Optional inputs (environment):
  TAG             image tag (default: canopy:dev)
  PLATFORM        target platform, e.g. linux/amd64 (default: host architecture)
  CANOPY_VERSION  PEP 440 version of canopy (default: setuptools_scm on this checkout)
  PYDM_VERSION    PEP 440 version of PyDMConverter (default: setuptools_scm on PYDM)

Options:
  --no-trace  ship an empty /app/trace (TRACE and TRACE_REF are then ignored)
  --worktree  build Trace from TRACE's working tree (uncommitted edits included)
              instead of a `git archive` of TRACE_REF, which must be TRACE's
              HEAD; label canopy, runtime and trace with working-tree
              fingerprints (<sha>+dirty.<fp>) that verify-image.sh recomputes
  --dry-run   validate inputs, resolve SHAs and print the docker build argv; build nothing
  -h, --help  show this help
EOF
}

die() { echo "$PROG: $*" >&2; exit 1; }

DRY_RUN=0
NO_TRACE=0
WORKTREE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-trace) NO_TRACE=1 ;;
        --worktree) WORKTREE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# --- validate inputs ---------------------------------------------------------
required=(PYDM RT)
[ "$NO_TRACE" = 1 ] || required+=(TRACE TRACE_REF)
for var in "${required[@]}"; do
    [ -n "${!var:-}" ] || die "set $var (see --help)"
done
dirs=(PYDM RT)
[ "$NO_TRACE" = 1 ] || dirs+=(TRACE)
for var in "${dirs[@]}"; do
    [ -d "${!var}" ] || die "$var=${!var} is not a directory"
    git -C "${!var}" rev-parse --verify --quiet HEAD >/dev/null \
        || die "$var=${!var} is not a git checkout with a commit"
done
[ -f "$DOCKERFILE" ] || die "missing $DOCKERFILE"

TAG="${TAG:-canopy:dev}"
PLATFORM="${PLATFORM:-}"

# HEAD of a checkout, with `+dirty` when a tracked file differs from HEAD
# (staged or not). Untracked files do not count.
head_sha() {
    local sha
    sha="$(git -C "$1" rev-parse --verify HEAD)"
    if git -C "$1" diff --quiet HEAD --; then echo "$sha"; else echo "$sha+dirty"; fi
}

sha256_stdin() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1
    fi
}

# --- working-tree provenance (keep identical to scripts/verify-image.sh) -------
# The files a working tree contributes: tracked and untracked, not ignored, not
# deleted, .claude/ (session state, never image content) excluded; NUL-separated.
tree_files() {
    (cd "$1" && git ls-files -z -c -o --exclude-standard -- . ':(exclude).claude' \
        | LC_ALL=C sort -zu \
        | while IFS= read -r -d '' f; do [ -f "$f" ] && printf '%s\0' "$f"; done)
}
# Content fingerprint of a working tree: sha256 over "<sha256>  <path>" lines.
tree_fp() {
    local hasher=(shasum -a 256)
    command -v sha256sum >/dev/null 2>&1 && hasher=(sha256sum)
    tree_files "$1" | (cd "$1" && xargs -0 "${hasher[@]}") | sha256_stdin | cut -c1-16
}
# HEAD of a checkout, or `<HEAD>+dirty.<tree_fp>` when the working tree has a
# tracked change (staged or not) or an untracked, non-ignored file.
tree_state() {
    local sha
    sha="$(git -C "$1" rev-parse --verify HEAD)"
    if [ -z "$(git -C "$1" status --porcelain --untracked-files=all -- . ':(exclude).claude')" ]; then
        echo "$sha"
    else
        echo "$sha+dirty.$(tree_fp "$1")"
    fi
}

if [ "$WORKTREE" = 1 ]; then
    CANOPY_SHA="$(tree_state "$ROOT")"
    RUNTIME_SHA="$(tree_state "$RT")"
else
    CANOPY_SHA="$(head_sha "$ROOT")"
    RUNTIME_SHA="$(head_sha "$RT")"
fi
git -C "$PYDM" diff --quiet HEAD -- \
    || die "PYDM=$PYDM has tracked changes; commit or discard them (the wheel is built from HEAD)"
PYDM_SHA="$(git -C "$PYDM" rev-parse --verify HEAD)"

TRACE_SHA=""
TRACE_LOCK_SHA256=""
if [ "$NO_TRACE" = 0 ]; then
    TRACE_SHA="$(git -C "$TRACE" rev-parse --verify --quiet "${TRACE_REF}^{commit}")" \
        || die "TRACE_REF=$TRACE_REF does not resolve to a commit in TRACE=$TRACE"
    git -C "$TRACE" cat-file -e "$TRACE_SHA:pnpm-lock.yaml" 2>/dev/null \
        || die "TRACE_REF=$TRACE_REF ($TRACE_SHA) has no pnpm-lock.yaml"
    if [ "$WORKTREE" = 1 ]; then
        [ "$(git -C "$TRACE" rev-parse --verify HEAD)" = "$TRACE_SHA" ] \
            || die "--worktree: TRACE_REF=$TRACE_REF ($TRACE_SHA) is not the HEAD of TRACE=$TRACE"
        [ -f "$TRACE/pnpm-lock.yaml" ] || die "--worktree: $TRACE/pnpm-lock.yaml is missing"
        TRACE_SHA="$(tree_state "$TRACE")"
        TRACE_LOCK_SHA256="$(sha256_stdin < "$TRACE/pnpm-lock.yaml")"
    else
        TRACE_LOCK_SHA256="$(git -C "$TRACE" show "$TRACE_SHA:pnpm-lock.yaml" | sha256_stdin)"
    fi
fi

echo "$PROG: canopy  $CANOPY_SHA ($ROOT)" >&2
echo "$PROG: runtime $RUNTIME_SHA ($RT)" >&2
echo "$PROG: pydm    $PYDM_SHA ($PYDM)" >&2
if [ "$NO_TRACE" = 0 ]; then
    mode="commit"; [ "$WORKTREE" = 1 ] && mode="working tree"
    echo "$PROG: trace   $TRACE_SHA ($TRACE_REF in $TRACE, $mode), pnpm-lock.yaml sha256 $TRACE_LOCK_SHA256" >&2
else
    echo "$PROG: trace   none (--no-trace)" >&2
fi

# PEP 440 versions from the host checkouts (Docker sees no usable .git).
scm_version() { (cd "$1" && uvx --from setuptools-scm python -m setuptools_scm); }
CANOPY_VERSION="${CANOPY_VERSION:-$(scm_version "$ROOT")}"
PYDM_VERSION="${PYDM_VERSION:-$(scm_version "$PYDM")}"
echo "$PROG: canopy $CANOPY_VERSION, PyDMConverter $PYDM_VERSION" >&2

# --- build contexts (all under one temp dir, removed on exit) ----------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/canopy-image.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PYDM_SRC="$WORK/pydm-src"
WHEEL_DIR="$WORK/wheels"
TRACE_SRC="$WORK/trace-src"
TRACE_EMPTY="$WORK/trace-empty"
VIEWER_DIST="$RT/apps/viewer/dist"

argv=(docker build)
[ -n "$PLATFORM" ] && argv+=(--platform "$PLATFORM")
argv+=(
    -f "$DOCKERFILE"
    --build-context "canopy=$ROOT"
    --build-context "pydm-src=$PYDM_SRC"
    --build-context "runtime=$RT"
    --build-context "viewer=$VIEWER_DIST"
    --build-context "pydm=$WHEEL_DIR"
    --build-context "trace-src=$TRACE_SRC"
)
[ "$NO_TRACE" = 1 ] && argv+=(--build-context "trace=$TRACE_EMPTY")
argv+=(
    --build-arg "CANOPY_VERSION=$CANOPY_VERSION"
    --build-arg "PYDM_VERSION=$PYDM_VERSION"
    --build-arg "CANOPY_SHA=$CANOPY_SHA"
    --build-arg "RUNTIME_SHA=$RUNTIME_SHA"
    --build-arg "TRACE_SHA=$TRACE_SHA"
    --build-arg "TRACE_LOCK_SHA256=$TRACE_LOCK_SHA256"
    -t "$TAG"
    "$ROOT/docker"
)

if [ "$DRY_RUN" = 1 ]; then
    printf '%q ' "${argv[@]}"
    echo
    exit 0
fi

mkdir -p "$PYDM_SRC" "$WHEEL_DIR" "$TRACE_SRC"

# PyDMConverter wheel from a pristine copy of PYDM HEAD (overrides stage `pydm`).
# The copy has no .git, so the version must be pretended.
git -C "$PYDM" archive "$PYDM_SHA" | tar -x -C "$PYDM_SRC"
(cd "$PYDM_SRC" && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYDMCONVERTER="$PYDM_VERSION" \
    uv build --wheel -o "$WHEEL_DIR")
ls "$WHEEL_DIR"/*.whl >/dev/null 2>&1 || die "no PyDMConverter wheel was built"

# Viewer dist (overrides stage `viewer`).
(cd "$RT" && pnpm --filter viewer build)
[ -f "$VIEWER_DIST/index.html" ] || die "viewer dist missing ($VIEWER_DIST/index.html)"

# react-trace source of exactly TRACE_SHA, or of the working tree under
# --worktree (built by the `trace-build` stage); under --no-trace both
# trace contexts stay empty.
if [ "$NO_TRACE" = 0 ] && [ "$WORKTREE" = 1 ]; then
    tree_files "$TRACE" | (cd "$TRACE" && tar --null -T - -cf -) | tar -x -C "$TRACE_SRC"
elif [ "$NO_TRACE" = 0 ]; then
    git -C "$TRACE" archive "${TRACE_SHA%%+*}" | tar -x -C "$TRACE_SRC"
else
    mkdir -p "$TRACE_EMPTY"
fi

"${argv[@]}"
echo "$PROG: built $TAG" >&2
