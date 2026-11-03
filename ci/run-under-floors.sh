#!/usr/bin/env bash
# Run tests against the oldest release of every direct dependency pyproject.toml admits.
#
#   ci/run-under-floors.sh <test file or directory>...
#
# The project is resolved with --resolution lowest-direct into an environment of its
# own, so a floor the tests do not pass at turns this run red while uv.lock keeps the
# versions a deployment installs. uv.lock is written by the resolution and restored
# on exit, whatever happens.
#
# Environment:
#   CANOPY_FLOORS_VENV    where the environment is built (default .cache/floors-venv)
#   CANOPY_FLOORS_PYTHON  interpreter request passed to uv (default 3.12)
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: ci/run-under-floors.sh <test file or directory>..." >&2
    exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export UV_PROJECT_ENVIRONMENT="${CANOPY_FLOORS_VENV:-.cache/floors-venv}"
PYTHON_REQUEST="${CANOPY_FLOORS_PYTHON:-3.12}"

WORK="$(mktemp -d)"
cp uv.lock "$WORK/uv.lock"
trap 'cp "$WORK/uv.lock" uv.lock; rm -rf "$WORK"' EXIT

# 1. the floors, resolved and installed with every extra and the test tools.
uv lock --resolution lowest-direct --python "$PYTHON_REQUEST"
uv sync --frozen --group dev --all-extras --python "$PYTHON_REQUEST"
uv export --frozen --no-hashes --no-emit-project --group dev --all-extras | grep -v '^#' | sort

# 2. the tests.
uv run --no-sync pytest -rs "$@"
