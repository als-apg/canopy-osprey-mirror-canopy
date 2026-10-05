#!/usr/bin/env bash
# Build the CANOPY payload: everything a host image needs to run CANOPY, as
# plain files in one directory, each built from one recorded commit per repo.
#
#   <out>/canopy-<version>-*.whl         canopy wheel
#   <out>/pydmconverter-<version>-*.whl  PyDMConverter wheel
#   <out>/viewer/                        canopy-screen-runtime viewer dist (index.html, ir-map.json)
#   <out>/trace/                         react-trace dist (empty under --no-trace)
#   <out>/widgets/*.json                 the widget registry (widget-registry-data/widgets)
#   <out>/STAGED.json                    the five commits and the two versions
#
# Nothing is built in a checkout. Every repo is exported with `git archive` at
# its commit into one temp root, laid out as
#   <root>/<repo dir name>/.claude/worktrees/canopy-osprey-connectors
# which is the depth at which the runtime's relative `link:` to the SDK
# resolves to the SDK export. An uncommitted edit in a checkout therefore
# neither blocks the build nor enters it, and the checkouts are left untouched.
#
# The exports have no .git, so the two Python versions are computed in the
# source checkouts first (setuptools_scm) and pretended to the wheel builds;
# a computed version whose +g<node> does not match the commit built is refused.
#
# STAGED.json is written last: a directory without it is not a payload.
#
# Needs git, tar, uv/uvx and pnpm. Run with --help for inputs.
set -euo pipefail

PROG=build-payload
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
LEAF=".claude/worktrees/canopy-osprey-connectors"

usage() {
    cat <<EOF
Usage: PYDM=<dir> RT=<dir> SDK=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-payload.sh <out>
       PYDM=<dir> RT=<dir> SDK=<dir> scripts/build-payload.sh --no-trace <out>

Builds the CANOPY payload into <out> (created if absent; must be empty) from
detached exports of one commit per repo. This canopy checkout is $ROOT.

Required inputs (environment):
  PYDM        pydm-converter-tool checkout
  RT          canopy-screen-runtime checkout (pnpm workspace with apps/viewer)
  SDK         canopy-sdk-js checkout (the runtime's link: target)
  TRACE       react-trace checkout (required unless --no-trace)
  TRACE_REF   git ref in TRACE to build (required unless --no-trace)

Optional inputs (environment):
  CANOPY_SHA      commit of this checkout to build (default: its HEAD)
  RT_SHA          commit of RT to build (default: its HEAD)
  SDK_SHA         commit of SDK to build (default: its HEAD)
  PYDM_SHA        commit of PYDM to build (default: its HEAD)
  CANOPY_VERSION  PEP 440 version of canopy (default: setuptools_scm on this checkout,
                  whose +g<node> must be a prefix of CANOPY_SHA)
  PYDM_VERSION    PEP 440 version of PyDMConverter (default: setuptools_scm on PYDM,
                  whose +g<node> must be a prefix of PYDM_SHA)

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
SDK_SHA="$(resolve SDK_SHA "$SDK" "${SDK_SHA:-HEAD}")"
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
echo "$PROG: sdk     $SDK_SHA ($SDK)" >&2
echo "$PROG: pydm    $PYDM_SHA ($PYDM)" >&2
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
    local dest="$EXPORTS/$3/$LEAF"
    mkdir -p "$dest"
    git -C "$1" archive "$2" | tar -x -C "$dest"
    echo "$dest"
}
CANOPY_SRC="$(export_repo "$ROOT" "$CANOPY_SHA" "$CANOPY_NAME")"
RT_SRC="$(export_repo "$RT" "$RT_SHA" "$RT_NAME")"
SDK_SRC="$(export_repo "$SDK" "$SDK_SHA" "$SDK_NAME")"
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
# The SDK first: the runtime (and Trace, below) resolve it from the SDK's dist.
# TRACE is unset for pnpm (it and the bundler read it as a tracing filter) and
# HUSKY=0 turns the `prepare: husky` hooks into no-ops (an export has no .git).
js() { env -u TRACE HUSKY=0 "$@"; }
(cd "$SDK_SRC" && js pnpm install --frozen-lockfile && js pnpm run build) \
    || die "building the SDK export of $SDK_SHA failed"
(cd "$RT_SRC" && js pnpm install --frozen-lockfile && js pnpm run build) \
    || die "building the runtime export of $RT_SHA failed"
VIEWER_DIST="$RT_SRC/apps/viewer/dist"
[ -s "$VIEWER_DIST/index.html" ] || die "viewer dist missing (apps/viewer/dist/index.html)"
[ -s "$VIEWER_DIST/ir-map.json" ] || die "viewer dist has no ir-map.json"
cp -R "$VIEWER_DIST" "$STAGE/viewer"

# --- trace -----------------------------------------------------------------------
if [ "$NO_TRACE" = 0 ]; then
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
    cp -R "$TRACE_SRC/dist" "$STAGE/trace"
else
    mkdir "$STAGE/trace"
fi

# --- widget registry ---------------------------------------------------------------
mkdir "$STAGE/widgets"
ls "$CANOPY_SRC"/widget-registry-data/widgets/*.json >/dev/null 2>&1 \
    || die "canopy $CANOPY_SHA has no widget-registry-data/widgets/*.json"
cp "$CANOPY_SRC"/widget-registry-data/widgets/*.json "$STAGE/widgets/"

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
  "pydm_version": $(json_str "$PYDM_VERSION")
}
EOF
mv "$OUT/STAGED.json.tmp" "$OUT/STAGED.json"
echo "$PROG: payload in $OUT" >&2
