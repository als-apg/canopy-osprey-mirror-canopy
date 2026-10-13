#!/usr/bin/env bash
# Build the CANOPY payload: everything a host image needs to run CANOPY, as
# plain files in one directory, each built from one recorded commit per repo.
#
#   <out>/canopy-<version>-*.whl         canopy wheel
#   <out>/pydmconverter-<version>-*.whl  PyDMConverter wheel
#   <out>/viewer/                        canopy-screen-runtime viewer dist (index.html, ir-map.json)
#   <out>/trace/                         react-trace dist (empty under --no-trace)
#   <out>/spec/                          the staged spec directory (only with SPEC set)
#   <out>/STAGED.json                    the five commits, the two versions and the spec
#
# Nothing is built in a checkout. Every repo is exported with `git archive` at
# its commit into one temp root, as <root>/<repo dir name>. An uncommitted edit
# in a checkout therefore neither blocks the build nor enters it, and the
# checkouts are left untouched.
#
# The runtime vendors the SDK as the submodule vendor/canopy-sdk-js, which
# `git archive` leaves empty. The SDK commit of a payload is therefore the one
# the runtime commit pins there: it is exported into that directory of the
# runtime export, where the runtime's own install builds it, and once more on
# its own for Trace, which links the SDK packages it does not declare.
#
# The exports have no .git, so the two Python versions are computed in the
# source checkouts first (setuptools_scm) and pretended to the wheel builds;
# a computed version whose +g<node> does not match the commit built is refused.
#
# STAGED.json is written last: a directory without it is not a payload.
#
# Needs git, tar, uv/uvx, pnpm, python3 and sha256sum or shasum. Run with
# --help for inputs.
set -euo pipefail

PROG=build-payload
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SDK_VENDOR="vendor/canopy-sdk-js"

usage() {
    cat <<EOF
Usage: PYDM=<dir> RT=<dir> SDK=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-payload.sh <out>
       PYDM=<dir> RT=<dir> SDK=<dir> scripts/build-payload.sh --no-trace <out>

Builds the CANOPY payload into <out> (created if absent; must be empty) from
detached exports of one commit per repo. This canopy checkout is $ROOT.

Required inputs (environment):
  PYDM        pydm-converter-tool checkout
  RT          canopy-screen-runtime checkout (pnpm workspace with apps/viewer)
  SDK         canopy-sdk-js checkout holding the commit the runtime pins
  TRACE       react-trace checkout (required unless --no-trace)
  TRACE_REF   git ref in TRACE to build (required unless --no-trace)

Optional inputs (environment):
  CANOPY_SHA      commit of this checkout to build (default: its HEAD)
  RT_SHA          commit of RT to build (default: its HEAD)
  SDK_SHA         commit of SDK to build; it must be the commit RT_SHA pins at
                  vendor/canopy-sdk-js, which is also the default
  PYDM_SHA        commit of PYDM to build (default: its HEAD)
  CANOPY_VERSION  PEP 440 version of canopy (default: setuptools_scm on this checkout,
                  whose +g<node> must be a prefix of CANOPY_SHA)
  PYDM_VERSION    PEP 440 version of PyDMConverter (default: setuptools_scm on PYDM,
                  whose +g<node> must be a prefix of PYDM_SHA)
  SPEC            staged spec directory (ir/, widgets/, SPEC-SOURCE naming repo and
                  SHA), copied to <out>/spec/ and recorded in STAGED.json as "spec"
                  (the SPEC-SOURCE SHA) and "spec-digest" (a digest of the tree);
                  unset means no spec/ and both recorded as null

Options:
  --no-trace  ship an empty trace/ (TRACE and TRACE_REF are then ignored)
  -h, --help  show this help
EOF
}

die() { echo "$PROG: $*" >&2; exit 1; }

NO_TRACE=0
OUT=""
for arg in "$@"; do
    case "$arg" in
        --no-trace) NO_TRACE=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "$PROG: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
        *)
            [ -z "$OUT" ] || { echo "$PROG: more than one output directory" >&2; usage >&2; exit 2; }
            OUT="$arg"
            ;;
    esac
done
[ -n "$OUT" ] || { echo "$PROG: missing output directory" >&2; usage >&2; exit 2; }

# --- validate inputs ---------------------------------------------------------
required=(PYDM RT SDK)
[ "$NO_TRACE" = 1 ] || required+=(TRACE TRACE_REF)
for var in "${required[@]}"; do
    [ -n "${!var:-}" ] || die "set $var (see --help)"
done
dirs=(PYDM RT SDK)
[ "$NO_TRACE" = 1 ] || dirs+=(TRACE)
for var in "${dirs[@]}"; do
    [ -d "${!var}" ] || die "$var=${!var} is not a directory"
    git -C "${!var}" rev-parse --verify --quiet HEAD >/dev/null \
        || die "$var=${!var} is not a git checkout with a commit"
done
git -C "$ROOT" rev-parse --verify --quiet HEAD >/dev/null \
    || die "$ROOT is not a git checkout with a commit"

# The staged spec: a directory whose SPEC-SOURCE names the commit it came from.
SPEC_SHA=""
if [ -n "${SPEC:-}" ]; then
    [ -d "$SPEC" ] || die "SPEC=$SPEC is not a directory"
    [ -f "$SPEC/SPEC-SOURCE" ] || die "SPEC=$SPEC has no SPEC-SOURCE"
    SPEC_SHA="$(grep -oE '(^|[^0-9a-f])[0-9a-f]{40}([^0-9a-f]|$)' "$SPEC/SPEC-SOURCE" | grep -oE '[0-9a-f]{40}' | head -n 1 || true)"
    [ -n "$SPEC_SHA" ] || die "SPEC=$SPEC: SPEC-SOURCE names no 40-hex commit"
fi

if [ -e "$OUT" ]; then
    [ -d "$OUT" ] || die "<out>=$OUT is not a directory"
    [ -z "$(ls -A "$OUT")" ] || die "<out>=$OUT is not empty"
fi

# The full commit a ref names in a checkout; dies naming the variable it came from.
resolve() { # <var name> <checkout> <ref>
    git -C "$2" rev-parse --verify --quiet "$3^{commit}" \
        || die "$1=$3 does not resolve to a commit in $2"
}
CANOPY_SHA="$(resolve CANOPY_SHA "$ROOT" "${CANOPY_SHA:-HEAD}")"
RT_SHA="$(resolve RT_SHA "$RT" "${RT_SHA:-HEAD}")"
# One SDK commit per payload: the one the runtime commit pins, so the viewer
# and Trace are built from the SDK the runtime was proven with.
SDK_PIN="$(git -C "$RT" ls-tree "$RT_SHA" -- "$SDK_VENDOR" | awk '$1 == "160000" && $2 == "commit" { print $3 }')"
[ -n "$SDK_PIN" ] \
    || die "RT_SHA=$RT_SHA has no submodule entry at $SDK_VENDOR; the payload needs a runtime commit that vendors the SDK there"
git -C "$SDK" rev-parse --verify --quiet "$SDK_PIN^{commit}" >/dev/null \
    || die "the SDK commit $SDK_PIN that RT_SHA=$RT_SHA pins at $SDK_VENDOR is not in SDK=$SDK (fetch it there)"
if [ -n "${SDK_SHA:-}" ]; then
    SDK_SHA="$(resolve SDK_SHA "$SDK" "$SDK_SHA")"
    [ "$SDK_SHA" = "$SDK_PIN" ] \
        || die "SDK_SHA=$SDK_SHA is not the commit RT_SHA=$RT_SHA pins at $SDK_VENDOR ($SDK_PIN); leave SDK_SHA unset or move the pin"
else
    SDK_SHA="$SDK_PIN"
fi
PYDM_SHA="$(resolve PYDM_SHA "$PYDM" "${PYDM_SHA:-HEAD}")"
TRACE_SHA=""
[ "$NO_TRACE" = 1 ] || TRACE_SHA="$(resolve TRACE_REF "$TRACE" "$TRACE_REF")"

# A checkout's directory name: the repository's, also when the checkout is a
# linked worktree somewhere below it.
repo_name() {
    local common
    common="$(cd "$1" && cd "$(git rev-parse --git-common-dir)" && pwd)"
    if [ "$(basename "$common")" = .git ]; then
        basename "$(dirname "$common")"
    else
        basename "$(git -C "$1" rev-parse --show-toplevel)"
    fi
}
CANOPY_NAME="$(repo_name "$ROOT")"
RT_NAME="$(repo_name "$RT")"
SDK_NAME="$(repo_name "$SDK")"
PYDM_NAME="$(repo_name "$PYDM")"
TRACE_NAME=""
[ "$NO_TRACE" = 1 ] || TRACE_NAME="$(repo_name "$TRACE")"
names="$(printf '%s\n' "$CANOPY_NAME" "$RT_NAME" "$SDK_NAME" "$PYDM_NAME" $TRACE_NAME)"
[ -z "$(printf '%s\n' "$names" | sort | uniq -d)" ] \
    || die "two checkouts share a directory name ($(printf '%s\n' "$names" | sort | uniq -d | tr '\n' ' ')); their exports would collide"

echo "$PROG: canopy  $CANOPY_SHA ($ROOT)" >&2
echo "$PROG: runtime $RT_SHA ($RT)" >&2
echo "$PROG: sdk     $SDK_SHA ($SDK, pinned by the runtime at $SDK_VENDOR)" >&2
echo "$PROG: pydm    $PYDM_SHA ($PYDM)" >&2
if [ -n "$SPEC_SHA" ]; then
    echo "$PROG: spec    $SPEC_SHA ($SPEC)" >&2
else
    echo "$PROG: spec    none (SPEC unset)" >&2
fi
if [ "$NO_TRACE" = 0 ]; then
    echo "$PROG: trace   $TRACE_SHA ($TRACE_REF in $TRACE)" >&2
else
    echo "$PROG: trace   none (--no-trace)" >&2
fi

# PEP 440 versions from the source checkouts (an export has no .git).
# setuptools_scm describes the checkout's HEAD, not the recorded commit, so a
# computed version must name the commit that is built: its local `+g<node>`
# has to be a prefix of that commit's SHA, or the wheel would claim a commit it
# was not built from. A version passed in explicitly is used as given.
scm_version() { (cd "$1" && uvx --from setuptools-scm python -m setuptools_scm); }
NODE_RE='\+g([0-9a-f]+)(\.|$)'
pick_version() { # <label> <given version> <checkout> <sha> <variable name>; prints the version
    local version node
    if [ -n "$2" ]; then
        echo "$PROG: $1 version $2 (given by $5, used as is)" >&2
        printf '%s\n' "$2"
        return
    fi
    version="$(scm_version "$3")" || die "setuptools_scm failed in $3 (set $5)"
    [ -n "$version" ] || die "no $1 version (set $5)"
    [[ "$version" =~ $NODE_RE ]] \
        || die "$1 version $version from setuptools_scm has no +g<commit>; it cannot be matched to $4 (set $5)"
    node="${BASH_REMATCH[1]}"
    [ "${4#"$node"}" != "$4" ] \
        || die "$1 version $version from setuptools_scm names +g$node, but the commit built is $4 (set $5, or build the checkout's HEAD)"
    echo "$PROG: $1 version $version (setuptools_scm, +g$node matches $4)" >&2
    printf '%s\n' "$version"
}
CANOPY_VERSION="$(pick_version canopy "${CANOPY_VERSION:-}" "$ROOT" "$CANOPY_SHA" CANOPY_VERSION)"
PYDM_VERSION="$(pick_version PyDMConverter "${PYDM_VERSION:-}" "$PYDM" "$PYDM_SHA" PYDM_VERSION)"

# --- detached exports (one temp root, removed on exit) -----------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/canopy-payload.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
WORK="$(cd "$WORK" && pwd -P)"
EXPORTS="$WORK/exports"
STAGE="$WORK/payload"
mkdir -p "$STAGE"

export_repo() { # <checkout> <sha> <repo dir name>; prints the export's path
    local dest="$EXPORTS/$3"
    mkdir -p "$dest"
    git -C "$1" archive "$2" | tar -x -C "$dest"
    echo "$dest"
}
CANOPY_SRC="$(export_repo "$ROOT" "$CANOPY_SHA" "$CANOPY_NAME")"
RT_SRC="$(export_repo "$RT" "$RT_SHA" "$RT_NAME")"
# The runtime export's submodule directory is empty; it gets the pinned SDK tree.
mkdir -p "$RT_SRC/$SDK_VENDOR"
git -C "$SDK" archive "$SDK_SHA" | tar -x -C "$RT_SRC/$SDK_VENDOR"
SDK_SRC=""
[ "$NO_TRACE" = 1 ] || SDK_SRC="$(export_repo "$SDK" "$SDK_SHA" "$SDK_NAME")"
PYDM_SRC="$(export_repo "$PYDM" "$PYDM_SHA" "$PYDM_NAME")"
TRACE_SRC=""
[ "$NO_TRACE" = 1 ] || TRACE_SRC="$(export_repo "$TRACE" "$TRACE_SHA" "$TRACE_NAME")"

# --- wheels ------------------------------------------------------------------
# hatch-vcs does not hand setuptools_scm the distribution name, so canopy's
# build reads the generic variable; it is set for that command alone.
(cd "$CANOPY_SRC" && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CANOPY="$CANOPY_VERSION" \
    SETUPTOOLS_SCM_PRETEND_VERSION="$CANOPY_VERSION" \
    uv build --wheel -o "$STAGE") || die "building the canopy wheel failed"
(cd "$PYDM_SRC" && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYDMCONVERTER="$PYDM_VERSION" \
    uv build --wheel -o "$STAGE") || die "building the PyDMConverter wheel failed"
ls "$STAGE"/canopy-*.whl >/dev/null 2>&1 || die "no canopy wheel was built"
[ "$(ls "$STAGE"/*.whl | wc -l | tr -d ' ')" = 2 ] \
    || die "expected a canopy and a PyDMConverter wheel, got: $(cd "$STAGE" && ls)"

# --- viewer --------------------------------------------------------------------
# The runtime's install builds the SDK packages it vendors, then its own.
# TRACE is unset for pnpm (it and the bundler read it as a tracing filter) and
# HUSKY=0 turns the `prepare: husky` hooks into no-ops (an export has no .git).
js() { env -u TRACE HUSKY=0 "$@"; }
(cd "$RT_SRC" && js pnpm install --frozen-lockfile && js pnpm run build) \
    || die "building the runtime export of $RT_SHA (SDK $SDK_SHA) failed"
VIEWER_DIST="$RT_SRC/apps/viewer/dist"
[ -s "$VIEWER_DIST/index.html" ] || die "viewer dist missing (apps/viewer/dist/index.html)"
[ -s "$VIEWER_DIST/ir-map.json" ] || die "viewer dist has no ir-map.json"
# The widget catalog is ir-map.json alone: every entry must carry its editor metadata.
missing="$(python3 -I -c '
import json, sys
entries = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(k for k, v in entries.items() if not (isinstance(v, dict) and isinstance(v.get("displayName"), str) and v["displayName"])))
' "$VIEWER_DIST/ir-map.json")" || die "cannot read the viewer's ir-map.json"
[ -z "$missing" ] || die "viewer ir-map.json entries without displayName: $missing"
cp -R "$VIEWER_DIST" "$STAGE/viewer"

# --- trace -----------------------------------------------------------------------
if [ "$NO_TRACE" = 0 ]; then
    (cd "$SDK_SRC" && js pnpm install --frozen-lockfile && js pnpm run build) \
        || die "building the SDK export of $SDK_SHA failed"
    (cd "$TRACE_SRC" && js pnpm install --frozen-lockfile) \
        || die "installing the Trace export of $TRACE_SHA failed"
    # Until the SDK is released, Trace imports SDK packages that its committed
    # package.json does not declare (its checkout carries an uncommitted link:).
    # Each SDK package the install did not provide is linked to the SDK export
    # built above, so Trace bundles the payload's own SDK commit. A Trace that
    # declares the package gets it from its lockfile and no link is made.
    for pkg in "$SDK_SRC"/packages/*/; do
        [ -f "$pkg/package.json" ] || continue
        name="$(sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' "$pkg/package.json" | head -n 1)"
        [ -n "$name" ] || continue
        [ ! -e "$TRACE_SRC/node_modules/$name" ] || continue
        mkdir -p "$(dirname "$TRACE_SRC/node_modules/$name")"
        ln -s "${pkg%/}" "$TRACE_SRC/node_modules/$name"
        echo "$PROG: trace   $name -> sdk $SDK_SHA" >&2
    done
    (cd "$TRACE_SRC" && js pnpm exec tsc -b && js pnpm exec vite build --base=./) \
        || die "building the Trace export of $TRACE_SHA failed"
    [ -s "$TRACE_SRC/dist/index.html" ] || die "trace dist missing (dist/index.html)"
    # Trace's own `build` script ends with this check; the steps above replace
    # that script, so the check is run here, on the dist that is shipped.
    (cd "$TRACE_SRC" && js pnpm run check-dist) \
        || die "the Trace build of $TRACE_SHA failed check-dist (external font references in dist)"
    cp -R "$TRACE_SRC/dist" "$STAGE/trace"
else
    mkdir "$STAGE/trace"
fi

# --- spec ---------------------------------------------------------------------------
# The digest covers every file's path and bytes, so a payload whose only change is
# the staged spec is still a new payload to the stager.
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; }
tree_digest() { # <dir>: sha256 over "<file sha256>  <relative path>" lines, sorted by path
    (cd "$1" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do
        printf '%s  %s\n' "$(sha256 < "$f" | cut -d' ' -f1)" "$f"
    done) | sha256 | cut -d' ' -f1
}
SPEC_DIGEST=""
if [ -n "$SPEC_SHA" ]; then
    mkdir "$STAGE/spec"
    cp -R "$SPEC"/. "$STAGE/spec"/
    SPEC_DIGEST="sha256:$(tree_digest "$STAGE/spec")"
fi

# --- hand over ---------------------------------------------------------------------
json_str() { if [ -n "$1" ]; then printf '"%s"' "$1"; else printf 'null'; fi; }
mkdir -p "$OUT"
cp -R "$STAGE"/. "$OUT"/
cat > "$OUT/STAGED.json.tmp" <<EOF
{
  "canopy": $(json_str "$CANOPY_SHA"),
  "runtime": $(json_str "$RT_SHA"),
  "sdk": $(json_str "$SDK_SHA"),
  "pydm": $(json_str "$PYDM_SHA"),
  "trace": $(json_str "$TRACE_SHA"),
  "canopy_version": $(json_str "$CANOPY_VERSION"),
  "pydm_version": $(json_str "$PYDM_VERSION"),
  "spec": $(json_str "$SPEC_SHA"),
  "spec-digest": $(json_str "$SPEC_DIGEST")
}
EOF
mv "$OUT/STAGED.json.tmp" "$OUT/STAGED.json"
echo "$PROG: payload in $OUT" >&2
