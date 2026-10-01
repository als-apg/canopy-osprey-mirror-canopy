#!/usr/bin/env bash
# Verify a CANOPY appliance image against the checkouts it should come from.
#
#   TRACE=<react-trace checkout> TRACE_REF=<ref> RT=<runtime checkout> \
#       scripts/verify-image.sh <image>
#
# Exits 0 only when every check passes:
#   - org.canopy.sha                    == this canopy checkout's HEAD (no +dirty)
#   - org.canopy.runtime.sha minus +dirty == RT HEAD
#   - org.canopy.trace.sha              == the commit TRACE_REF resolves to in TRACE
#   - org.canopy.trace.lockfile-sha256  == sha256 of that commit's pnpm-lock.yaml
#   - in the image: /app/viewer/index.html and /app/trace/index.html exist, the
#     trace index uses relative ./assets/ URLs (no ="/assets/ or ="/favicon.svg),
#     and `import pymongo` works.
# An image built with `build-image.sh --worktree` carries `<sha>+dirty.<fp>`
# labels instead; each is accepted only when the checkout's HEAD is <sha> and
# its working tree still hashes to <fp> (canopy, trace), and for such a trace
# label the lockfile hash is taken from TRACE's working-tree pnpm-lock.yaml.
# A non-zero exit therefore means "rebuild with scripts/build-image.sh".
set -euo pipefail

PROG=verify-image
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

usage() {
    cat <<EOF
Usage: TRACE=<dir> TRACE_REF=<ref> RT=<dir> scripts/verify-image.sh <image>

  TRACE      react-trace checkout the image's Trace was built from
  TRACE_REF  ref in TRACE whose commit the image must carry
  RT         canopy-screen-runtime checkout the viewer was built from
  <image>    image to check, e.g. canopy:dev
EOF
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac

missing=()
for var in TRACE TRACE_REF RT; do
    [ -n "${!var:-}" ] || missing+=("$var")
done
if [ "${#missing[@]}" -gt 0 ] || [ "$#" -ne 1 ]; then
    [ "${#missing[@]}" -gt 0 ] && echo "$PROG: set ${missing[*]}" >&2
    [ "$#" -ne 1 ] && echo "$PROG: expected exactly one image argument" >&2
    usage >&2
    exit 2
fi
IMAGE="$1"

sha256_stdin() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1
    fi
}

# --- working-tree provenance (keep identical to scripts/build-image.sh) --------
tree_files() {
    (cd "$1" && git ls-files -z -c -o --exclude-standard -- . ':(exclude).claude' \
        | LC_ALL=C sort -zu \
        | while IFS= read -r -d '' f; do [ -f "$f" ] && printf '%s\0' "$f"; done)
}
tree_fp() {
    local hasher=(shasum -a 256)
    command -v sha256sum >/dev/null 2>&1 && hasher=(sha256sum)
    tree_files "$1" | (cd "$1" && xargs -0 "${hasher[@]}") | sha256_stdin | cut -c1-16
}
tree_state() {
    local sha
    sha="$(git -C "$1" rev-parse --verify HEAD)"
    if [ -z "$(git -C "$1" status --porcelain --untracked-files=all -- . ':(exclude).claude')" ]; then
        echo "$sha"
    else
        echo "$sha+dirty.$(tree_fp "$1")"
    fi
}

TRACE_SHA="$(git -C "$TRACE" rev-parse --verify --quiet "${TRACE_REF}^{commit}")" \
    || { echo "$PROG: TRACE_REF=$TRACE_REF does not resolve in TRACE=$TRACE" >&2; exit 1; }
LOCK_SHA256="$(git -C "$TRACE" show "$TRACE_SHA:pnpm-lock.yaml" | sha256_stdin)"
CAN_HEAD="$(git -C "$ROOT" rev-parse --verify HEAD)"
RT_HEAD="$(git -C "$RT" rev-parse --verify HEAD)"

docker image inspect "$IMAGE" >/dev/null 2>&1 \
    || { echo "$PROG: image $IMAGE not found" >&2; exit 1; }

label() { docker image inspect -f "{{ index .Config.Labels \"$1\" }}" "$IMAGE"; }

fail=0
check() {  # check <what> <got> <want>
    if [ "$2" = "$3" ]; then
        echo "$PROG: ok   $1 = $2"
    else
        echo "$PROG: FAIL $1 = '$2', expected '$3'" >&2
        fail=1
    fi
}

canopy_label="$(label org.canopy.sha)"
case "$canopy_label" in
    *+dirty.*) check "org.canopy.sha (working tree)" "$canopy_label" "$(tree_state "$ROOT")" ;;
    *) check org.canopy.sha "$canopy_label" "$CAN_HEAD" ;;
esac
runtime_label="$(label org.canopy.runtime.sha)"
check "org.canopy.runtime.sha (minus +dirty)" "${runtime_label%%+dirty*}" "$RT_HEAD"
trace_label="$(label org.canopy.trace.sha)"
case "$trace_label" in
    *+dirty.*)
        check "TRACE HEAD (working-tree build of $TRACE_REF)" "$(git -C "$TRACE" rev-parse --verify HEAD)" "$TRACE_SHA"
        check "org.canopy.trace.sha (working tree)" "$trace_label" "$(tree_state "$TRACE")"
        WT_LOCK="$(sha256_stdin < "$TRACE/pnpm-lock.yaml")"
        check "org.canopy.trace.lockfile-sha256 (working tree)" "$(label org.canopy.trace.lockfile-sha256)" "$WT_LOCK"
        ;;
    *)
        check org.canopy.trace.sha "$trace_label" "$TRACE_SHA"
        check org.canopy.trace.lockfile-sha256 "$(label org.canopy.trace.lockfile-sha256)" "$LOCK_SHA256"
        ;;
esac

# In-container checks; each failing line is printed by the container.
if docker run --rm --entrypoint sh "$IMAGE" -c '
    rc=0
    for f in /app/viewer/index.html /app/trace/index.html; do
        [ -f "$f" ] || { echo "missing $f"; rc=1; }
    done
    t=/app/trace/index.html
    if [ -f "$t" ]; then
        grep -qF "./assets/" "$t" || { echo "$t has no ./assets/ reference"; rc=1; }
        ! grep -qF "=\"/assets/" "$t" || { echo "$t has an absolute /assets/ URL"; rc=1; }
        ! grep -qF "=\"/favicon.svg" "$t" || { echo "$t has an absolute /favicon.svg URL"; rc=1; }
    fi
    python -c "import pymongo" || { echo "import pymongo failed"; rc=1; }
    exit $rc
' >&2; then
    echo "$PROG: ok   image files, trace asset URLs, import pymongo"
else
    echo "$PROG: FAIL in-image checks (see above)" >&2
    fail=1
fi

if [ "$fail" = 0 ]; then
    echo "$PROG: $IMAGE matches the checkouts"
else
    echo "$PROG: $IMAGE does NOT match; rebuild with scripts/build-image.sh" >&2
fi
exit "$fail"
