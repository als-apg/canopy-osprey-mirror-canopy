#!/usr/bin/env bash
# Build the Canopy payload: the files a host image needs to run Canopy in
# appliance mode, each built from one recorded commit per repository.
#
#   PYDM=<dir> RT=<dir> SDK=<dir> SDK_SHA=<commit> SDK_PACKS=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-payload.sh <out>
#   PYDM=<dir> RT=<dir> SDK=<dir> SDK_SHA=<commit> SDK_PACKS=<dir> scripts/build-payload.sh --no-trace <out>
#
# <out> receives the canopy and PyDMConverter wheels, viewer/, trace/, spec/
# (only with SPEC set) and STAGED.json, written last: a directory without it is
# not a payload. Every repository is exported with `git archive` at its commit,
# so nothing is built in a checkout and an uncommitted edit never enters the
# build. Before anything is built, the fixtures Canopy shares with the runtime
# and the SDK are compared at the recorded commits (check_shared_fixtures). The
# runtime and Trace take the SDK from packs (SDK_PACKS, `pnpm pack` of SDK_SHA,
# in a directory or fetched from a release with its SHA512SUMS), never from a
# registry; once installed, their lockfiles must resolve one SDK, the packs of
# the release tagged at SDK_SHA (check_one_sdk), before anything of theirs is
# built.
#
# What the payload holds and how a host uses it: docs/deployment/appliance.md.
# Inputs and checks: --help. Needs git, tar, uv/uvx, pnpm, python3, sha256sum or
# shasum, and curl when SDK_PACKS is a URL. The pnpm on PATH runs, and it must be
# the version the commits' packageManager names (a commit cannot switch it here).
set -euo pipefail

PROG=build-payload
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# No registry is read, so no registry token reaches a commit's install.
unset NODE_AUTH_TOKEN

usage() {
    cat <<EOF
Usage: PYDM=<dir> RT=<dir> SDK=<dir> SDK_SHA=<commit> SDK_PACKS=<dir> TRACE=<dir> TRACE_REF=<ref> scripts/build-payload.sh <out>
       PYDM=<dir> RT=<dir> SDK=<dir> SDK_SHA=<commit> SDK_PACKS=<dir> scripts/build-payload.sh --no-trace <out>

Builds the CANOPY payload into <out> (created if absent; must be empty) from
detached exports of one commit per repo. This canopy checkout is $ROOT.

Required inputs (environment):
  PYDM        pydm-converter-tool checkout
  RT          canopy-screen-runtime checkout (pnpm workspace with apps/viewer)
  SDK         canopy-sdk-js checkout holding SDK_SHA
  SDK_SHA     the commit of SDK tagged v<version> for the @slaclab/canopy-* version
              the packs carry; it has no default
  SDK_PACKS   directory of the SDK's packs, slaclab-canopy-<package>-<version>.tgz
              (pnpm pack of SDK_SHA; a prerelease such as 0.3.1-als.1 included),
              one per package, one version; the runtime and Trace install them
              in place of any registry copy. Or an https:// (or file://) URL of a
              release, e.g. https://github.com/<owner>/<repo>/releases/download/<tag>
              (a .../releases/tag/<tag> page URL is read as that): its SHA512SUMS
              and every pack it lists are fetched. A SHA512SUMS (lines
              "<sha512 hex>  <pack>"), required from a release and optional in a
              directory, must list every pack with its digest, and nothing else
  TRACE       react-trace checkout (required unless --no-trace)
  TRACE_REF   git ref in TRACE to build (required unless --no-trace)

Optional inputs (environment):
  CANOPY_SHA      commit of this checkout to build (default: its HEAD)
  RT_SHA          commit of RT to build (default: its HEAD)
  PYDM_SHA        commit of PYDM to build (default: its HEAD)
  CANOPY_VERSION  PEP 440 version of canopy (default: setuptools_scm at CANOPY_SHA,
                  in a throwaway clone of this checkout; a tag must be reachable
                  from CANOPY_SHA: canopy's release tags, v0.0.1 onward, are in
                  https://github.com/slaclab/canopy)
  PYDM_VERSION    PEP 440 version of PyDMConverter (default: setuptools_scm at
                  PYDM_SHA, in a throwaway clone of PYDM; a tag must be reachable
                  from PYDM_SHA)
  SPEC            staged spec directory (ir/, widgets/, SPEC-SOURCE naming repo and
                  SHA), copied to <out>/spec/ and recorded in STAGED.json as "spec"
                  (the SPEC-SOURCE SHA) and "spec-digest" (a digest of the tree);
                  unset means no spec/ and both recorded as null

pnpm runs as found on PATH; a commit's packageManager never switches it. When the
runtime or Trace commit names pnpm@<version> there, the pnpm on PATH must be that
version, or the build is refused before any install.

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
  vectors     heartbeat_vectors.json and write_outcome_vectors.json in
              src/canopy/contracts/ at CANOPY_SHA must each equal their copy in
              packages/client/test-vectors/ at SDK_SHA byte for byte; a file at
              only one of the two commits is refused; STAGED.json records
              "vectors" as "compared", or "absent" when neither commit holds
              either

  one SDK     after the installs (which resolve @slaclab/canopy-* to the packs),
              the pnpm-lock.yaml of the runtime and Trace exports must resolve
              exactly client, react, screen-ir and shell (runtime), charts,
              client, react, shell and vite (Trace), each to a pack in
              SDK_PACKS with that pack's integrity, and nothing else; any
              lockfile line naming @slaclab that is not recognised is refused.
              Every package both resolve must have the same version and
              integrity, each version must be the one packages/*/package.json
              holds at SDK_SHA, and SDK must hold the tag v<version> naming
              SDK_SHA; STAGED.json records them as "sdk_packages". Runs before
              the viewer or Trace is built

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
required=(PYDM RT SDK SDK_SHA SDK_PACKS)
[ "$NO_TRACE" = 1 ] || required+=(TRACE TRACE_REF)
for var in "${required[@]}"; do
    [ -n "${!var:-}" ] || die "set $var (see --help)"
done
dirs=(PYDM RT SDK)
# A release URL is fetched once the temp root exists (fetch_packs).
SDK_PACKS_URL=""
case "$SDK_PACKS" in
    https://*|file://*)
        SDK_PACKS_URL="${SDK_PACKS%/}"
        case "$SDK_PACKS_URL" in */releases/tag/*)
            SDK_PACKS_URL="${SDK_PACKS_URL%%/releases/tag/*}/releases/download/${SDK_PACKS_URL#*/releases/tag/}" ;;
        esac
        ;;
    *)
        [ -d "$SDK_PACKS" ] || die "SDK_PACKS=$SDK_PACKS is not a directory or an https:// or file:// URL"
        SDK_PACKS="$(cd "$SDK_PACKS" && pwd)"
        ;;
esac
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
SDK_SHA="$(resolve SDK_SHA "$SDK" "$SDK_SHA")"
PYDM_SHA="$(resolve PYDM_SHA "$PYDM" "${PYDM_SHA:-HEAD}")"
TRACE_SHA=""
if [ "$NO_TRACE" = 0 ]; then
    TRACE_SHA="$(resolve TRACE_REF "$TRACE" "$TRACE_REF")"
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
#   vectors      each of VECTOR_FILES in src/canopy/contracts/ at canopy and
#                packages/client/test-vectors/ at the SDK commit; compared when
#                either commit holds the file.
#
# check_shared_fixtures sets FIXTURES, WORKSPACE and VECTORS to "compared" or
# "absent" (recorded in STAGED.json), or dies naming the first differing path.
FIX_CANOPY="tests/canopy_mcp/fixtures/collections"
FIX_RT="apps/viewer/e2e/fixtures/collections"
WS_CANOPY="tests/canopy_mcp/fixtures/workspace"
WS_RT="apps/viewer/e2e/fixtures/workspace"
VEC_CANOPY="src/canopy/contracts"
VEC_SDK="packages/client/test-vectors"
VECTOR_FILES="heartbeat_vectors.json write_outcome_vectors.json" # C-sorted: the first refused is the first differing

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
echo "$PROG: sdk     $SDK_SHA ($SDK)" >&2
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

# --- detached exports (one temp root, removed on exit) -----------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/canopy-payload.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
WORK="$(cd "$WORK" && pwd -P)"
EXPORTS="$WORK/exports"
STAGE="$WORK/payload"
mkdir -p "$STAGE"

# PEP 440 versions from the source checkouts (an export has no .git).
# setuptools_scm describes HEAD, so it runs at the recorded commit in a
# throwaway clone (sharing the checkout's objects, holding its tags), never in
# the checkout itself. Its version counts from the nearest tag the commit
# descends from: a commit with no tag in reach, or a shallow checkout, would get
# a made-up version, so either is refused. The local `+g<node>` must still be a
# prefix of the commit built. A version passed in explicitly is used as given.
scm_version() { (cd "$1" && uvx --from setuptools-scm python -m setuptools_scm); }
NODE_RE='\+g([0-9a-f]+)(\.|$)'
pick_version() { # <label> <given version> <checkout> <sha> <variable name> <where its tags are>; prints the version
    local version node at tag
    if [ -n "$2" ]; then
        echo "$PROG: $1 version $2 (given by $5, used as is)" >&2
        printf '%s\n' "$2"
        return
    fi
    [ "$(git -C "$3" rev-parse --is-shallow-repository)" = false ] \
        || die "$3 is a shallow clone: setuptools_scm cannot count the commits since the $1 tag (fetch with --unshallow, or set $5)"
    at="$WORK/scm/$1"
    git clone --quiet --shared --no-checkout "$3" "$at" >&2 \
        && git -C "$at" -c advice.detachedHead=false checkout --quiet --detach "$4" >&2 \
        || die "cannot check out $4 of $3 to compute the $1 version (set $5)"
    tag="$(git -C "$at" describe --tags --abbrev=0 HEAD 2>/dev/null)" \
        || die "no tag in $3 is reachable from $4, so setuptools_scm would make up the $1 version; fetch the release tag it descends from ($6: git -C $3 fetch <that repository> tag <tag>), or set $5"
    version="$(scm_version "$at")" || die "setuptools_scm failed at $4 of $3 (set $5)"
    rm -rf "$at"
    [ -n "$version" ] || die "no $1 version (set $5)"
    [[ "$version" =~ $NODE_RE ]] \
        || die "$1 version $version from setuptools_scm has no +g<commit>; it cannot be matched to $4 (set $5)"
    node="${BASH_REMATCH[1]}"
    [ "${4#"$node"}" != "$4" ] \
        || die "$1 version $version from setuptools_scm names +g$node, but the commit built is $4 (set $5)"
    echo "$PROG: $1 version $version (setuptools_scm at $4, from tag $tag)" >&2
    printf '%s\n' "$version"
}
CANOPY_VERSION="$(pick_version canopy "${CANOPY_VERSION:-}" "$ROOT" "$CANOPY_SHA" CANOPY_VERSION \
    "canopy's are v0.0.1 onward in https://github.com/slaclab/canopy")"
PYDM_VERSION="$(pick_version PyDMConverter "${PYDM_VERSION:-}" "$PYDM" "$PYDM_SHA" PYDM_VERSION \
    "PyDMConverter's are v1.0.0 onward in https://github.com/slaclab/pydm-converter-tool")"

# --- SDK packs: a release's are fetched, a SHA512SUMS is checked -------------
# SHA512SUMS lines are "<sha512 hex>  <pack>" (sha512sum's form; " *" also read).
# With "list", prints the packs it names; otherwise every pack in <dir> must be
# listed with its digest, and every listed pack must be in <dir>.
check_sums() { # <dir> [list]
    python3 -I -c '
import hashlib, os, re, sys
directory, mode = sys.argv[1], sys.argv[2]
PACK = r"slaclab-canopy-[a-z]+(?:-[a-z]+)*-[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\.tgz"
def refuse(message):
    sys.exit(f"build-payload: {message}")
sums = {}
with open(os.path.join(directory, "SHA512SUMS"), encoding="utf-8") as f:
    for number, line in enumerate(f, 1):
        if not line.strip():
            continue
        entry = re.fullmatch(rf"([0-9a-f]{{128}}) [ *]({PACK})", line.rstrip("\n"))
        if not entry:
            refuse(f"SHA512SUMS line {number} is not \"<sha512 hex>  slaclab-canopy-<package>-<version>.tgz\": {line.strip()}")
        sums[entry.group(2)] = entry.group(1)
if not sums:
    refuse("SHA512SUMS lists no slaclab-canopy-*.tgz pack")
if mode == "list":
    print("\n".join(sorted(sums)))
    sys.exit()
held = sorted(name for name in os.listdir(directory) if re.fullmatch(PACK, name))
for name in held:
    if name not in sums:
        refuse(f"pack {name} is not listed in SHA512SUMS")
    with open(os.path.join(directory, name), "rb") as f:
        if hashlib.sha512(f.read()).hexdigest() != sums[name]:
            refuse(f"pack {name} does not match its SHA512SUMS digest")
for name in sorted(set(sums) - set(held)):
    refuse(f"SHA512SUMS lists {name}, which is not among the packs")
' "$1" "${2:-verify}"
}
fetch() { curl -fsSL --proto '=https,file' --proto-redir '=https' -o "$2" "$1"; }
fetch_packs() { # <release url> <dir>
    local names name
    mkdir -p "$2"
    fetch "$1/SHA512SUMS" "$2/SHA512SUMS" || die "cannot fetch $1/SHA512SUMS (SDK_PACKS names a release, which must hold one)"
    names="$(check_sums "$2" list)" || exit 1
    for name in $names; do
        fetch "$1/$name" "$2/$name" || die "cannot fetch $1/$name, listed in its SHA512SUMS"
    done
}
if [ -n "$SDK_PACKS_URL" ]; then
    SDK_PACKS="$WORK/packs"
    fetch_packs "$SDK_PACKS_URL" "$SDK_PACKS"
    echo "$PROG: packs   fetched from $SDK_PACKS_URL" >&2
fi
if [ -e "$SDK_PACKS/SHA512SUMS" ]; then
    check_sums "$SDK_PACKS" || exit 1
    echo "$PROG: packs   match their SHA512SUMS" >&2
fi

export_repo() { # <checkout> <sha> <repo dir name>; prints the export's path
    local dest="$EXPORTS/$3"
    mkdir -p "$dest"
    git -C "$1" archive "$2" | tar -x -C "$dest"
    echo "$dest"
}
CANOPY_SRC="$(export_repo "$ROOT" "$CANOPY_SHA" "$CANOPY_NAME")"
RT_SRC="$(export_repo "$RT" "$RT_SHA" "$RT_NAME")"
PYDM_SRC="$(export_repo "$PYDM" "$PYDM_SHA" "$PYDM_NAME")"
TRACE_SRC=""
[ "$NO_TRACE" = 1 ] || TRACE_SRC="$(export_repo "$TRACE" "$TRACE_SHA" "$TRACE_NAME")"

# --- one SDK per payload ------------------------------------------------------
# The viewer and Trace take the SDK from the packs in SDK_PACKS. A payload holds
# one SDK, the release tagged at SDK_SHA, so both re-resolved lockfiles must
# resolve its packs. This runs on the exports after the installs and before any
# build. The script's Python has no YAML parser, so the lockfile is read by line:
# a pack reference is read as the version it carries, and a line naming @slaclab
# in any other form but the ones pnpm writes for a package is refused.
# This checks consistency and is not a security boundary: whoever controls a commit
# controls its build. Prints the JSON recorded as "sdk_packages", or refuses on stderr.
check_one_sdk() { # <runtime export> <Trace export, or "">
    python3 -I -c '
import base64, hashlib, json, os, re, subprocess, sys

sdk, sha, rt_sha, trace_sha, rt_src, trace_src, packs = sys.argv[1:]
RUNTIME_PACKAGES = {"@slaclab/canopy-" + p for p in ("client", "react", "screen-ir", "shell")}
TRACE_PACKAGES = {"@slaclab/canopy-" + p for p in ("charts", "client", "react", "shell", "vite")}
NAME = r"@slaclab/canopy-[a-z-]+"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?"
PACK = rf"slaclab-canopy-([a-z]+(?:-[a-z]+)*)-({VERSION})\.tgz"
PEERS = r"(?:\(.*\))?"
ENTRY_FIELDS = {"peerDependencies", "peerDependenciesMeta", "engines", "hasBin", "os", "cpu", "libc"}

def refuse(message):
    sys.exit(f"build-payload: {message}")

def git(*args):
    return subprocess.run(["git", "-C", sdk, *args], capture_output=True, text=True)

def read(label, directory, name):
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        refuse(f"the {label} has no readable {name}")

def resolved(label, directory, wanted, held):
    text = read(label, directory, "pnpm-lock.yaml")
    # A pack installed through an override reads as the version it carries: the
    # override block, the file: specifiers and the tarball fields are dropped.
    # Only the forms that name a pack (slaclab-canopy-<package>-<version>.tgz) go.
    text = re.sub(rf"^overrides:\n(?:  \x27{NAME}\x27: file:\S*?/{PACK}\n)+\n?", "", text, flags=re.M)
    text = re.sub(rf"(resolution: \{{integrity: [^,}}]+), tarball: file:[^}}]*?/{PACK}\}}\n    version: [^\n]*\n", r"\1}\n", text)
    text = re.sub(rf"(resolution: \{{integrity: [^,}}]+), tarball: file:[^}}]*?/{PACK}\}}", r"\1}", text)
    text = re.sub(rf"file:[^\s\x27(,}}]*?/{PACK}", r"\2", text)
    lines = text.splitlines()
    if not lines or lines[0] != "lockfileVersion: \x279.0\x27":
        refuse(f"the {label} pnpm-lock.yaml is not lockfile version 9.0, the only form this check reads")

    def bad(number, line, why):
        refuse(f"the {label} pnpm-lock.yaml, line {number}, {why}: {line.strip()}")

    found, used, section, entry, importer, peers = {}, [], None, None, None, False
    for number, line in enumerate(lines, 1):
        names_us = "slaclab" in line
        if not line.strip():
            continue
        if not line.startswith(" "):
            section, entry = line.partition(":")[0], None
            if names_us or importer:
                bad(number, line, "is not a form this check reads")
        elif importer:
            importer["fields"].append(line.strip())
            if len(importer["fields"]) == 2:
                name, (specifier, version) = importer["name"], importer["fields"]
                if not (specifier.startswith("specifier: ") and version.startswith("version: ")):
                    bad(number, line, f"is not a form this check reads for {name}")
                specifier, version = specifier.partition(": ")[2], version.partition(": ")[2]
                plain = re.fullmatch(f"({VERSION}){PEERS}", version)
                if name in wanted:
                    if not (re.fullmatch(rf"[\^~]?{VERSION}", specifier) and plain):
                        bad(number, line, f"resolves {name} to something other than a registry version")
                    used.append((name, plain.group(1)))
                elif name in held or not (specifier.startswith("workspace:") and version.startswith("link:")):
                    bad(number, line, f"{name} is neither in the fixed set nor a workspace package")
                importer = None
        elif section == "importers":
            if names_us:
                key = re.fullmatch(rf"      \x27({NAME})\x27:", line)
                if not key:
                    bad(number, line, "is not a form this check reads")
                importer = {"name": key.group(1), "fields": []}
        elif section == "packages":
            if not line.startswith("   "):
                entry, peers = None, False
                if names_us:
                    key = re.fullmatch(rf"  \x27({NAME})@({VERSION})\x27:", line)
                    if not key:
                        bad(number, line, "does not resolve to a plain registry version")
                    if key.group(1) not in wanted:
                        bad(number, line, "is not in the fixed package set")
                    entry = {"version": key.group(2), "integrity": None}
                    found.setdefault(key.group(1), []).append(entry)
            elif names_us:
                # A package of the set may name another as a peer (charts names shell).
                peer = re.fullmatch(rf"      \x27({NAME})\x27: [\^~]?{VERSION}", line)
                if not (entry and peers and peer):
                    bad(number, line, "is not a form this check reads")
                if peer.group(1) not in wanted:
                    bad(number, line, "names a peer outside the fixed package set")
            elif entry and not line.startswith("     "):
                field = re.match(r"    ([A-Za-z]+):", line)
                peers = bool(field) and field.group(1) == "peerDependencies"
                integrity = re.fullmatch(r"    resolution: \{integrity: (sha512-[A-Za-z0-9+/=]+)\}", line)
                if integrity:
                    entry["integrity"] = integrity.group(1)
                elif not field or field.group(1) not in ENTRY_FIELDS:
                    bad(number, line, "has a field other than a registry integrity")
        elif section == "snapshots":
            if names_us:
                key = re.fullmatch(rf"  \x27({NAME})@({VERSION}){PEERS}\x27:(?: \{{\}})?", line)
                dependency = re.fullmatch(rf"      \x27({NAME})\x27: ({VERSION}){PEERS}", line)
                if not (key or dependency):
                    bad(number, line, "is not a form this check reads")
                used.append((key or dependency).groups())
        elif names_us:
            bad(number, line, f"names the SDK in section {section}")

    missing = sorted(wanted - set(found))
    if missing:
        absent = ", ".join(missing)
        refuse(f"the {label} pnpm-lock.yaml does not resolve {absent}; run pnpm install there against the released SDK and commit the lockfile")
    for name, entries in found.items():
        if len(entries) > 1:
            versions = ", ".join(e["version"] for e in entries)
            refuse(f"the {label} pnpm-lock.yaml resolves {name} to more than one version ({versions}); a payload has one SDK")
        if not entries[0]["integrity"]:
            refuse(f"the {label} pnpm-lock.yaml has no registry integrity for {name}")
    for name, version in used:
        if name not in found or found[name][0]["version"] != version:
            refuse(f"the {label} pnpm-lock.yaml uses {name}@{version}, which its packages section does not resolve")
    return {name: entries[0] for name, entries in sorted(found.items())}

def spec(entry):
    return "{version} ({integrity})".format(**entry)

# The packs: one per package, one version, each with the integrity pnpm records.
pack_integrity = {}
for entry in sorted(os.listdir(packs)):
    found_pack = re.fullmatch(PACK, entry)
    if not found_pack:
        continue
    name, version = "@slaclab/canopy-" + found_pack.group(1), found_pack.group(2)
    if name in pack_integrity:
        refuse(f"SDK_PACKS={packs} holds more than one pack of {name}")
    with open(os.path.join(packs, entry), "rb") as f:
        digest = base64.b64encode(hashlib.sha512(f.read()).digest()).decode()
    pack_integrity[name] = {"version": version, "integrity": "sha512-" + digest}

# The release: the package metadata at SDK_SHA, and its tag.
held = {}
for directory in git("ls-tree", "--name-only", sha, "packages/").stdout.split():
    shown = git("show", f"{sha}:{directory}/package.json")
    if shown.returncode == 0:
        meta = json.loads(shown.stdout)
        held[meta.get("name")] = meta.get("version")

runtime = resolved(f"runtime (RT_SHA={rt_sha})", rt_src, RUNTIME_PACKAGES, held)
trace = resolved(f"Trace (TRACE_SHA={trace_sha})", trace_src, TRACE_PACKAGES, held) if trace_src else None

for name in sorted(set(runtime) & set(trace or {})):
    if runtime[name] != trace[name]:
        refuse(f"RT_SHA={rt_sha} resolves {name} to {spec(runtime[name])}, but TRACE_SHA={trace_sha} resolves it to {spec(trace[name])}; a payload has one SDK, so move one lockfile to match the other")

both = {**(trace or {}), **runtime}
for name, entry in both.items():
    if pack_integrity.get(name) != entry:
        refuse(f"{name} is resolved to {spec(entry)}, which is not a pack in SDK_PACKS={packs}")
for name, entry in both.items():
    version = entry["version"]
    if name not in held:
        refuse(f"{name}@{version} is resolved, but SDK_SHA={sha} has no such package under packages/")
    if held[name] != version:
        refuse(f"{name} is resolved to {version}, but SDK_SHA={sha} holds {held[name]}; SDK_SHA must be the commit released as that version")
for version in sorted({entry["version"] for entry in both.values()}):
    tagged = git("rev-parse", "--verify", "--quiet", f"refs/tags/v{version}^{{commit}}").stdout.strip()
    if not tagged:
        refuse(f"tag v{version} is not in {sdk}; SDK_SHA must be the commit released as that version (fetch its tags)")
    if tagged != sha:
        refuse(f"tag v{version} names {tagged} in {sdk}, but SDK_SHA={sha}; SDK_SHA must be the commit released as that version")

print(json.dumps({"runtime": runtime, "trace": trace}, sort_keys=True))
' "$SDK" "$SDK_SHA" "$RT_SHA" "$TRACE_SHA" "$1" "$2" "$SDK_PACKS"
}

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

# --- installs ------------------------------------------------------------------
# TRACE is unset for pnpm (it and the bundler read it as a tracing filter) and
# HUSKY=0 turns the `prepare: husky` hooks into no-ops (an export has no .git).
# The install reads an empty .npmrc written here (a commit's own .npmrc never
# applies), runs no package scripts or pnpmfile, and no commit picks the pnpm
# that runs. Every @slaclab/canopy-* package is overridden by its pack, so the
# lockfile is re-resolved (not frozen) and checked right after (check_one_sdk);
# a pack is no registry release, so registry release-age policies skip it.
js() { env -u TRACE HUSKY=0 pnpm_config_manage_package_manager_versions=false "$@"; }
js_install() { # in an export's directory, in a subshell
    rm -f .npmrc
    : > .npmrc
    local names=()
    {
        echo
        echo "overrides:"
        for pack in "$SDK_PACKS"/slaclab-canopy-*.tgz; do
            [ -e "$pack" ] || continue
            name="$(basename "$pack")"
            name="${name#slaclab-}"
            name="${name%%-[0-9]*.tgz}"
            names+=("$name")
            echo "  '@slaclab/$name': file:$pack"
        done
        echo
        echo "minimumReleaseAgeExclude:"
        for name in "${names[@]}"; do
            echo "  - '@slaclab/$name'"
        done
    } >> pnpm-workspace.yaml
    js pnpm install --no-frozen-lockfile --ignore-scripts --ignore-pnpmfile
}
ls "$SDK_PACKS"/slaclab-canopy-*.tgz >/dev/null 2>&1 || die "SDK_PACKS=$SDK_PACKS holds no slaclab-canopy-*.tgz pack"
PNPM_VERSION="$(cd "$RT_SRC" && js pnpm --version)" || die "pnpm --version failed"
pnpm_named() { # <export>: the version its packageManager names for pnpm, or nothing
    python3 -I -c '
import json, sys
manager = json.load(open(sys.argv[1], encoding="utf-8")).get("packageManager") or ""
print(manager[len("pnpm@"):].split("+")[0] if manager.startswith("pnpm@") else "")
' "$1/package.json"
}
check_pnpm() { # <label> <export>
    local named
    named="$(pnpm_named "$2")" || die "cannot read packageManager in the $1 export"
    [ -z "$named" ] || [ "$named" = "$PNPM_VERSION" ] \
        || die "pnpm $PNPM_VERSION is on PATH, but the $1 names pnpm@$named in packageManager; put pnpm $named first on PATH"
}
check_pnpm "runtime (RT_SHA=$RT_SHA)" "$RT_SRC"
[ "$NO_TRACE" = 1 ] || check_pnpm "Trace (TRACE_SHA=$TRACE_SHA)" "$TRACE_SRC"
echo "$PROG: pnpm    $PNPM_VERSION" >&2
(cd "$RT_SRC" && js_install) \
    || die "installing the runtime export of $RT_SHA failed (pnpm install --no-frozen-lockfile --ignore-scripts --ignore-pnpmfile)"
if [ "$NO_TRACE" = 0 ]; then
    (cd "$TRACE_SRC" && js_install) \
        || die "installing the Trace export of $TRACE_SHA failed (pnpm install --no-frozen-lockfile --ignore-scripts --ignore-pnpmfile)"
fi

SDK_PACKAGES="$(check_one_sdk "$RT_SRC" "$TRACE_SRC")" || exit 1
echo "$PROG: one sdk  resolved from the packs by the runtime and Trace, tagged at $SDK_SHA" >&2

# --- viewer --------------------------------------------------------------------
(cd "$RT_SRC" && js pnpm run build) \
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
  "sdk_packages": $SDK_PACKAGES,
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
