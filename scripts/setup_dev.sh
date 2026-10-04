#!/usr/bin/env bash
set -e

uv sync --group dev --all-extras
uv run pre-commit install
uv lock
