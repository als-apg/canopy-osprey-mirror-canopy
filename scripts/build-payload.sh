#!/usr/bin/env bash
# Build the Canopy payload: the files a host image needs to run Canopy in
# appliance mode, each built from one recorded commit per repository.
#
#   PYDM=<dir> RT=<dir> SDK=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-payload.sh <out>
#   PYDM=<dir> RT=<dir> SDK=<dir> scripts/build-payload.sh --no-trace <out>
#
# <out> receives the canopy and PyDMConverter wheels, viewer/, trace/, spec/
# (only with SPEC set) and STAGED.json, written last: a directory without it is
# not a payload. Every repository is exported with `git archive` at its commit,
# so nothing is built in a checkout and an uncommitted edit never enters the
# build. Before anything is built, the fixtures Canopy shares with the runtime
# and the SDK are compared at the recorded commits (check_shared_fixtures).
#
# What the payload holds and how a host uses it: docs/deployment/appliance.md.
# Inputs and checks: --help. Needs git, tar, uv/uvx, pnpm, python3 and
# sha256sum or shasum.
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
  TRACE_REF   git ref in TRACE to build (required unless --no-trace); it must
              pin the same SDK commit at vendor/canopy-sdk-js as RT_SHA

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

Checks (before anything is built; each refuses the build on a mismatch):
  fixtures    when RT_SHA has apps/viewer/e2e/fixtures/collections/, its
              member-key-vectors.json, match-vectors.json, valid/, invalid/ and
              responses/ must equal tests/canopy_mcp/fixtures/collections/ at
              CANOPY_SHA byte for byte (library/ is not compared);
              STAGED.json records "fixtures" as "compared", or "absent" when
              RT_SHA has no such directory
  workspace   tests/canopy_mcp/fixtures/workspace/ at CANOPY_SHA and
              apps/viewer/e2e/fixtures/workspace/ at RT_SHA must be equal byte
              for byte, every file, and a copy on one side only is refused;
              STAGED.json records "workspace" as "compared", or "absent" when
              CANOPY_SHA has no such directory (and RT_SHA has none either)
  vectors     src/canopy/contracts/wc6_vectors.json at CANOPY_SHA must equal
              packages/client/test-vectors/wc6_vectors.json at SDK_SHA byte for
              byte; a file at only one of the two commits is refused;
              STAGED.json records "vectors" as "compared", or "absent" when
              neither commit holds it

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
# The SDK commit a commit pins at vendor/canopy-sdk-js (its gitlink); empty
# when that commit has no submodule entry there.
sdk_pin() { # <checkout> <sha>
    git -C "$1" ls-tree "$2" -- "$SDK_VENDOR" | awk '$1 == "160000" && $2 == "commit" { print $3 }'
}
# One SDK commit per payload: the one the runtime commit pins, so the viewer
# and Trace are built from the SDK the runtime was proven with.
SDK_PIN="$(sdk_pin "$RT" "$RT_SHA")"
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
if [ "$NO_TRACE" = 0 ]; then
    TRACE_SHA="$(resolve TRACE_REF "$TRACE" "$TRACE_REF")"
    TRACE_PIN="$(sdk_pin "$TRACE" "$TRACE_SHA")"
    [ -n "$TRACE_PIN" ] \
        || die "TRACE_REF=$TRACE_REF ($TRACE_SHA) has no submodule entry at $SDK_VENDOR: it predates the vendored SDK, and a payload needs a Trace commit that vendors the SDK there (build a later Trace commit, or use --no-trace)"
    [ "$TRACE_PIN" = "$SDK_PIN" ] \
        || die "TRACE_REF=$TRACE_REF ($TRACE_SHA) pins SDK $TRACE_PIN at $SDK_VENDOR, but RT_SHA=$RT_SHA pins SDK $SDK_PIN; a payload has one SDK commit, so move one gitlink to match the other"
fi

# --- shared fixtures: refuse drifted copies ----------------------------------
# Some wire contracts are held in more than one repository: Canopy's copy is
# the source, and the runtime and the SDK keep byte copies for their own tests.
# A payload built from commits whose copies disagree would ship a viewer tested
# against another contract than the server it talks to, so the build is refused.
# The copies are compared at the recorded commits (never a working tree), by
# object id: equal blob ids are equal bytes.
#
#   collections  tests/canopy_mcp/fixtures/collections/ at canopy and
#                apps/viewer/e2e/fixtures/collections/ at the runtime: the
#                member-key-vectors.json, match-vectors.json, valid/, invalid/
#                and responses/ set (library/ is canopy's alone); compared when
#                the runtime commit has the directory.
#   workspace    tests/canopy_mcp/fixtures/workspace/ at canopy and
#                apps/viewer/e2e/fixtures/workspace/ at the runtime: every
#                file, both ways; compared when either commit has the directory.
#   vectors      src/canopy/contracts/wc6_vectors.json at canopy and
#                packages/client/test-vectors/wc6_vectors.json at the SDK
#                commit; compared when either commit holds the file.
#
# check_shared_fixtures sets FIXTURES, WORKSPACE and VECTORS to "compared" or
# "absent" (recorded in STAGED.json), or dies naming the first differing path.
FIX_CANOPY="tests/canopy_mcp/fixtures/collections"
FIX_RT="apps/viewer/e2e/fixtures/collections"
WS_CANOPY="tests/canopy_mcp/fixtures/workspace"
WS_RT="apps/viewer/e2e/fixtures/workspace"
VEC_CANOPY="src/canopy/contracts"
VEC_SDK="packages/client/test-vectors"
VECTOR_FILES="wc6_vectors.json" # C-sorted: the first refused is the first differing

# "<path below dir><TAB><type> <object>" per entry below <dir>, C-sorted.
# With <subset> set, only the compared collections set.
fixture_set() { # <checkout> <sha> <dir> [subset]
    git -C "$1" -c core.quotePath=false ls-tree -r --full-tree "$2" -- "$3/" \
        | awk -F'\t' -v dir="$3/" -v subset="${4:-}" '{
            split($1, m, " ")
            rel = substr($2, length(dir) + 1)
            if (subset == "" || rel == "member-key-vectors.json" || rel == "match-vectors.json" || rel ~ /^(valid|invalid|responses)\//)
                print rel "\t" m[2] " " m[3]
        }' \
        | LC_ALL=C sort
}
is_tree() { # <checkout> <sha> <dir>
    [ "$(git -C "$1" cat-file -t "$2:$3" 2>/dev/null || true)" = tree ]
}
# Dies naming the first path (in C order) where the two sets differ and which
# side has it; returns when they are equal.
same_sets() { # <label> <canopy dir> <rt dir> <canopy set> <rt set>
    local differing first_path in_canopy in_rt how
    [ "$4" != "$5" ] || return 0
    differing="$(LC_ALL=C comm -3 <(printf '%s\n' "$4") <(printf '%s\n' "$5") \
        | awk -F'\t' '{ p = ($1 == "" ? $2 : $1); if (p != "") print p }' | LC_ALL=C sort -u)"
    first_path="${differing%%$'\n'*}"
    in_canopy="$(printf '%s\n' "$4" | awk -F'\t' -v p="$first_path" '$1 == p { print "y" }')"
    in_rt="$(printf '%s\n' "$5" | awk -F'\t' -v p="$first_path" '$1 == p { print "y" }')"
    if [ -z "$in_rt" ]; then
        how="is in CANOPY_SHA=$CANOPY_SHA but not in RT_SHA=$RT_SHA"
    elif [ -z "$in_canopy" ]; then
        how="is in RT_SHA=$RT_SHA but not in CANOPY_SHA=$CANOPY_SHA"
    else
        how="differs between CANOPY_SHA=$CANOPY_SHA and RT_SHA=$RT_SHA"
    fi
    die "$1 fixture $first_path $how ($2/ vs $3/); the runtime's copy must be a byte copy of canopy's"
}
blob_at() { # <checkout> <sha> <path>: the blob's object id there, empty when there is none
    git -C "$1" -c core.quotePath=false ls-tree --full-tree "$2" -- "$3" \
        | awk -F'\t' '{ split($1, m, " "); if (m[2] == "blob") print m[3] }'
}
check_shared_fixtures() {
    local vector canopy_blob sdk_blob how
    if is_tree "$RT" "$RT_SHA" "$FIX_RT"; then
        same_sets collections "$FIX_CANOPY" "$FIX_RT" \
            "$(fixture_set "$ROOT" "$CANOPY_SHA" "$FIX_CANOPY" subset)" \
            "$(fixture_set "$RT" "$RT_SHA" "$FIX_RT" subset)"
        FIXTURES=compared
    else
        FIXTURES=absent
    fi

    # Compared whole and both ways: a copy on either side alone is refused, so
    # "absent" means neither commit has one.
    if is_tree "$ROOT" "$CANOPY_SHA" "$WS_CANOPY" || is_tree "$RT" "$RT_SHA" "$WS_RT"; then
        same_sets workspace "$WS_CANOPY" "$WS_RT" \
            "$(fixture_set "$ROOT" "$CANOPY_SHA" "$WS_CANOPY")" \
            "$(fixture_set "$RT" "$RT_SHA" "$WS_RT")"
        WORKSPACE=compared
    else
        WORKSPACE=absent
    fi

    VECTORS=absent
    for vector in $VECTOR_FILES; do
        canopy_blob="$(blob_at "$ROOT" "$CANOPY_SHA" "$VEC_CANOPY/$vector")"
        sdk_blob="$(blob_at "$SDK" "$SDK_SHA" "$VEC_SDK/$vector")"
        if [ -z "$canopy_blob" ] && [ -z "$sdk_blob" ]; then
            continue
        elif [ -z "$sdk_blob" ]; then
            how="is in CANOPY_SHA=$CANOPY_SHA but not in SDK_SHA=$SDK_SHA"
        elif [ -z "$canopy_blob" ]; then
            how="is in SDK_SHA=$SDK_SHA but not in CANOPY_SHA=$CANOPY_SHA"
        elif [ "$canopy_blob" != "$sdk_blob" ]; then
            how="differs between CANOPY_SHA=$CANOPY_SHA and SDK_SHA=$SDK_SHA"
        else
            VECTORS=compared
            continue
        fi
        die "vector file $vector $how ($VEC_CANOPY/ vs $VEC_SDK/); the SDK's copy must be a byte copy of canopy's"
    done
}
check_shared_fixtures

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
PYDM_NAME="$(repo_name "$PYDM")"
TRACE_NAME=""
[ "$NO_TRACE" = 1 ] || TRACE_NAME="$(repo_name "$TRACE")"
names="$(printf '%s\n' "$CANOPY_NAME" "$RT_NAME" "$PYDM_NAME" ${TRACE_NAME:+"$TRACE_NAME"})"
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
    echo "$PROG: trace   $TRACE_SHA ($TRACE_REF in $TRACE, pins the same sdk)" >&2
else
    echo "$PROG: trace   none (--no-trace)" >&2
fi
if [ "$FIXTURES" = compared ]; then
    echo "$PROG: fixtures compared ($FIX_CANOPY/ at canopy = $FIX_RT/ at runtime)" >&2
else
    echo "$PROG: fixtures absent (the runtime commit has no $FIX_RT/)" >&2
fi
if [ "$WORKSPACE" = compared ]; then
    echo "$PROG: workspace compared ($WS_CANOPY/ at canopy = $WS_RT/ at runtime)" >&2
else
    echo "$PROG: workspace absent (neither commit has $WS_CANOPY/ or $WS_RT/)" >&2
fi
if [ "$VECTORS" = compared ]; then
    echo "$PROG: vectors compared ($VEC_CANOPY/ at canopy = $VEC_SDK/ at sdk)" >&2
else
    echo "$PROG: vectors absent (neither commit holds $VECTOR_FILES)" >&2
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
# An export of a repo that vendors the SDK: its submodule directory, which
# `git archive` leaves empty, gets the tree of the payload's SDK commit (the one
# both the runtime and Trace commits were checked to pin above).
export_vendoring_sdk() { # <checkout> <sha> <repo dir name>; prints the export's path
    local dest
    dest="$(export_repo "$1" "$2" "$3")"
    mkdir -p "$dest/$SDK_VENDOR"
    git -C "$SDK" archive "$SDK_SHA" | tar -x -C "$dest/$SDK_VENDOR"
    echo "$dest"
}
CANOPY_SRC="$(export_repo "$ROOT" "$CANOPY_SHA" "$CANOPY_NAME")"
RT_SRC="$(export_vendoring_sdk "$RT" "$RT_SHA" "$RT_NAME")"
PYDM_SRC="$(export_repo "$PYDM" "$PYDM_SHA" "$PYDM_NAME")"
TRACE_SRC=""
[ "$NO_TRACE" = 1 ] || TRACE_SRC="$(export_vendoring_sdk "$TRACE" "$TRACE_SHA" "$TRACE_NAME")"

# --- wheels ------------------------------------------------------------------
# hatch-vcs does not hand setuptools_scm the distribution name, so canopy's
# build reads the generic variable; it is set for that command alone.
(cd "$CANOPY_SRC" && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CANOPY="$CANOPY_VERSION" \
    SETUPTOOLS_SCM_PRETEND_VERSION="$CANOPY_VERSION" \
    uv build --wheel -o "$STAGE") || die "building the canopy wheel failed"
(cd "$PYDM_SRC" && SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYDMCONVERTER="$PYDM_VERSION" \
    uv build --wheel -o "$STAGE") || die "building the PyDMConverter wheel failed"
ls "$STAGE"/canopy-*.whl >/dev/null 2>&1 || die "no canopy wheel was built"
[ "$(find "$STAGE" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')" = 2 ] \
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
    # Like the runtime's, Trace's install builds the SDK packages it vendors.
    (cd "$TRACE_SRC" && js pnpm install --frozen-lockfile) \
        || die "installing the Trace export of $TRACE_SHA (SDK $SDK_SHA) failed"
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
  "spec-digest": $(json_str "$SPEC_DIGEST"),
  "fixtures": $(json_str "$FIXTURES"),
  "workspace": $(json_str "$WORKSPACE"),
  "vectors": $(json_str "$VECTORS")
}
EOF
mv "$OUT/STAGED.json.tmp" "$OUT/STAGED.json"
echo "$PROG: payload in $OUT" >&2
