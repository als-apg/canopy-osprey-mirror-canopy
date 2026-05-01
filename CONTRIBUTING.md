# Contributing

## Local Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/slaclab/Canopy
cd Canopy
./scripts/setup_dev.sh
```

This installs dev dependencies and sets up pre-commit hooks.

## Running Tests

```bash
uv run pytest tests/ -v
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting, and [isort](https://pycqa.github.io/isort/) for import ordering. Pre-commit hooks run these automatically on every commit.

To run them manually:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run isort .
```

## Building Docs

```bash
uv run mkdocs serve
```

## Submitting Changes

1. Create a branch off `main` using the format `<username>/<description>` or `<username>/#<issue-number>-<description>`:
   ```
   zdomke/my-changes
   yektay/#10-bug-fix
   ```
2. Make your changes and ensure tests pass
3. Open a pull request using the provided template
