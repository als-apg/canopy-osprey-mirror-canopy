#!/usr/bin/env bash
# Run tests against the dependency versions an OSPREY appliance image ships.
#
#   ci/run-under-pins.sh <test file or directory>...
#
# canopy is resolved with the mcp and convert extras under the constraints in
# ci/osprey-pins.txt (the `pip freeze` of the image canopy is dropped into), so
# the code under test meets the fastmcp, starlette, uvicorn ... it will meet
# there, not the ones uv.lock would pick. The `dev` group and caproto are then
# layered on top as test-only tools: they are not pinned, and the run stops if
# installing them changed the version of anything the pinned step installed.
#
# The data modules of the appliance are selected (CANOPY_MODULES). A test
# skipped because a module could not be imported fails the run - under the pins
# that is a missing dependency, not an optional one. Skips for the environment
# (no live stack, no viewer build, no Chromium, an optional adapter method) are
# reported by -rs and allowed.
#
# Environment:
#   CANOPY_PINS_VENV    where the environment is built (default .cache/pins-venv)
#   CANOPY_PINS_PYTHON  interpreter request passed to uv (default 3.12)
#   CANOPY_PINS_CONVERTER  directory or URL of a PyDMConverter source to install in
#                       place of the released one (the appliance ships its own build)
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: ci/run-under-pins.sh <test file or directory>..." >&2
    exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PINS="ci/osprey-pins.txt"
VENV="${CANOPY_PINS_VENV:-.cache/pins-venv}"
PYTHON_REQUEST="${CANOPY_PINS_PYTHON:-3.12}"

uv venv --quiet --allow-existing --python "$PYTHON_REQUEST" "$VENV"
WORK="$(mktemp -d "$VENV/run.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PY="$VENV/bin/python"

# 1. canopy and its runtime dependencies, under the pins.
converter=()
if [ -n "${CANOPY_PINS_CONVERTER:-}" ]; then
    converter=("PyDMConverter @ $CANOPY_PINS_CONVERTER")
fi
uv pip install --quiet --python "$PY" -c "$PINS" -e ".[mcp,convert]" ${converter[@]+"${converter[@]}"}
uv pip freeze --python "$PY" | grep -v '^-e ' | sort > "$WORK/pinned.txt"

# 2. the test-only tools, with everything from step 1 held where it is.
uv pip install --quiet --python "$PY" -c "$WORK/pinned.txt" --group dev caproto
uv pip freeze --python "$PY" | grep -v '^-e ' | sort > "$WORK/layered.txt"

moved="$(comm -23 "$WORK/pinned.txt" "$WORK/layered.txt")"
if [ -n "$moved" ]; then
    echo "run-under-pins: the test tools moved packages of the pinned resolution:" >&2
    echo "$moved" >&2
    exit 1
fi

# 3. the tests, with the appliance's module selection.
export CANOPY_MODULES="hare,vole,fox,mole,converter"

status=0
"$PY" -m pytest -rs "$@" 2>&1 | tee "$WORK/pytest.log" || status="${PIPESTATUS[0]}"

missing="$(grep -E '^SKIPPED ' "$WORK/pytest.log" \
    | grep -iE 'No module named|could not import|importorskip|ModuleNotFoundError' || true)"
if [ -n "$missing" ]; then
    echo "run-under-pins: tests were skipped for a missing module:" >&2
    echo "$missing" >&2
    [ "$status" -ne 0 ] || status=1
fi

exit "$status"
