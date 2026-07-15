# Canopy — agent notes

This repo is the **backend** for Canopy, a control-system display platform being built at SLAC EED to replace PyDM 1.0. Frontend (React + Tauri + Carbon) lives in a separate repo and is out of scope here.

The project is in early-design / scaffolding state. The package tree under `src/` is still the unrenamed Python template (`eed_swapps_py_template/`); the real `src/canopy/` module tree has not been created yet. Treat most code-level references in design docs as planned, not implemented.

## Sources of truth (read these before designing)

The wiki is authoritative; the handoff doc is older and partially stale.

1. **GitHub wiki** — `slaclab/Canopy.wiki` (clone `https://github.com/slaclab/Canopy.wiki.git`). Pages:
   - `Architecture-Overview.md` — module conventions, communication rules, persistence, API conventions, deployment topology, extraction playbook.
   - `Component-Inventory-for-Canopy.md` — what each backend module does + the frontend systems they serve.
   - `proposed-file-structure.md` — target layout under `src/canopy/`.
   - `Vole:-Module-Architecture.md` — full design for Vole (the time-aligned buffer); also the template for what a per-module design page should look like.
2. **`canopy-handoff.md`** at the repo root — broader context (rename history, frontend overview, open design areas). Useful for the *why*; do not trust it for current module roles.
3. **Open issues** (`gh issue list`) — design tickets, one per module: Hare (#1), Mole (#2), Fox (#3), Vole (#4), Owl (#5).

When the handoff doc and the wiki disagree, the wiki wins.

### Known drift between handoff doc and wiki
- Handoff lists **Otter** as a service. It does not exist. Its role (time-aligned data buffer feeding Fox) is now **Vole**.
- Handoff implies five microservices. The current decision is a **modular monolith**: one FastAPI process, modules with hard internal boundaries, in-process gateway calls between them.
- Wren is **not** a standalone service — it runs as an internal pipeline stage inside Hare.
- Aliases for the project: PyDM 2.0 → Argus → **Canopy**. Use Canopy.

## Architecture in one paragraph

One FastAPI app (`canopy.main:app`) mounts per-module sub-apps at `/<module>`. Each module has the same internal shape: `api/` (routes, DTOs, deps) → `services/` (business logic, no FastAPI/SQLAlchemy imports) → `repositories/` + `adapters/` (data + external clients) → `models/` (SQLAlchemy ORM where needed). Cross-module calls go through `<module>/gateway.py` and only `gateway.py` — never reach into another module's `services/`, `repositories/`, or `data/`. Fire-and-forget cross-module work goes through `canopy.core.events` (in-process pub/sub, shaped like a real broker so it can be swapped for NATS/Kafka later). One Postgres instance, one schema per module that needs persistence; no cross-schema FKs, joins, or transactions. Redis is for cache, the Arq job queue, and WebSocket pub/sub fanout across replicas — *not* for inter-module communication. Two process types from the same image: `canopy-api` (uvicorn) and `canopy-worker` (Arq). Scale by replication.

## Module map

| Module | Role | Notes |
|---|---|---|
| **Hare** | Live PV WebSocket broker | FastAPI + WS. Hosts Wren as an internal pipeline stage. Backwards-compatible `ws://` for legacy PyDM screens. |
| **Wren** | Rules engine | Lives *inside* Hare. Evaluates expressions against PV updates, emits widget property changes. Expression language TBD. |
| **Mole** | Archiver Appliance proxy | REST over the raw archiver interface. Multiple archivers. |
| **Raccoon** | PV directory / channel finder | Pluggable directory adapters (e.g. `MemeDirectoryAdapter` for SLAC). LRU+TTL cache, single-flight. List/exists/validate routes. Powers Screen Builder PV picker, search, context menus. |
| **Fox** | Formula engine | Scalar + trace/curve + waveform. Stateless — pulls aligned windows from Vole. |
| **Vole** | Time-aligned in-memory buffer | RAM-only (no persistence). Per-PV rings, alignment strategies. Per-replica state — no distributed sync. See wiki page for full design. |
| **Squirrel** | Snapshot / restore / compare | Three-tier with Postgres persistence. |
| **Owl** (backend) | Plot config persistence + legacy importer | Imports `.trc` / `.xml` / `.stp` → plot configs. Owns saved plots for the frontend Owl tool. |
| **Jobs** | Arq + Redis worker pool | Long-running ops (exports, snapshots, restores) enqueue here. Clients poll `/jobs/v1/{id}`. |
| **EPICS Connector** | CA/PVA protocol layer | Low-level. Rust or C++ — TBD. The non-EPICS swap point for other labs. |

**Proposed but not yet a module:** a **Widget Registry** service (suggested codename "Beaver") to serve widget metadata + JSON Schemas to the Screen Builder, `.ui` converter, runtime renderer, and legacy PyQt bridge. File-based JSON-per-widget, FastAPI. Spec lives in `~/pydm_2/canopy-widget-registry-handoff.md` (frontend-driven, not yet on the backend roadmap).

## API conventions (cheat sheet, full text in wiki)

- Per-module sub-apps mounted at `/<module>`; each exposes `/<module>/v1/...`, `/health/{summary,watchdog}`, `/docs`.
- DTOs: Pydantic v2, all inheriting a shared `CanopyModel` base in `canopy.contracts.base` configured with `alias_generator=to_camel`, `validate_by_alias=True`, `validate_by_name=True`. Suffixes: `FooDTO` (response), `NewFooDTO` (POST body), `UpdateFooDTO` (PUT body), `FooSummaryDTO` (list item), `FooResultDTO` (op result). Pagination: `PagedResult[T]`.
- **Field casing:** snake_case in Python; `to_camel` alias generator emits camelCase on the wire and accepts either form on input. No per-field `Field(alias=...)` annotations.
- Resources are plural nouns; actions are POST to a sub-resource (`/snapshots/{id}/restore`).
- Auth: `X-API-Key` header. Every endpoint gated by `Security(require_read_access)` or `Security(require_write_access)`. WS variant: `ws_require_read_access`.
- **Errors:** services raise typed domain exceptions inheriting from `canopy.core.errors.CanopyError`, which carries `status_code` and `error_code: ErrorCode` (a shared `StrEnum` with module-specific slugs). A per-module `@app.exception_handler(CanopyError)` in `<module>/app.py` shapes every error response as `{"code": "<slug>", "detail": "<msg>"}`. Routes never raise `HTTPException` directly. Clients branch on `code`, not on parsing `detail`.
- Long-running ops return `JobCreatedDTO`; clients poll `/jobs/v1/{id}`.
- Centralize `Annotated[..., Depends(...)]` aliases (`PVServiceDep`, `MoleServiceDep`, `DataBaseDep`, `RedisServiceDep`) in `<module>/dependencies.py`; routes pull them in by alias rather than re-spelling `Depends()`.

> **Wiki drift to fix:** the wiki's `Architecture-Overview.md` currently describes "FastAPI default `{detail}`" envelope and "snake_case in Python via Pydantic field aliases." The above (`{code, detail}` slug envelope and `alias_generator=to_camel`) is the up-to-date convention — the wiki page needs a matching update on the next pass.

## Boundary enforcement

These are the rules CI is meant to enforce (linters not yet wired up — this is on the roadmap):
- import-linter / Ruff `flake8-tidy-imports` bans `from canopy.<other>.{services,repositories,data,models,adapters} import …` from outside that module. The only public surface is `<module>/gateway.py`.
- No foreign keys whose target lives in another schema; cross-module references are opaque IDs validated by gateway calls.
- No cross-schema joins; assemble cross-module data in service code that calls multiple gateways.
- No transactions spanning modules — use the event bus or accept eventual consistency.

When in doubt, check `Architecture-Overview.md` §3, §4, §8.

## Tooling and workflow

- **Python:** 3.12+. Package manager: **uv**.
- **Setup:** `./scripts/setup_dev.sh` (installs dev deps + pre-commit hooks).
- **Tests:** `uv run pytest tests/ -v`.
- **Lint/format:** ruff + isort, run via pre-commit. Manually: `uv run ruff check --fix . && uv run ruff format . && uv run isort .`. Line length 120. isort profile `black`.
- **Docs:** `uv run mkdocs serve` (Material theme, mkdocstrings for API reference).
- **Pre-commit hooks** run end-of-file-fixer, trailing-whitespace, ruff (with fix), ruff-format, isort. Don't bypass with `--no-verify`.
- **Branches:** `<username>/<description>` or `<username>/#<issue>-<description>` (e.g. `yektay/#10-bug-fix`).
- **Commits:** keep messages concise (one-line subject; body only when the *why* isn't obvious). See recent `git log` for style. **Do not add attribution trailers** (`Co-Authored-By:`, `Generated with …`, etc.) — clean history only.
- **No em-dashes (—) in comments, code, commit messages, or PR/issue text.** Use a regular hyphen, a colon, or split the sentence. (Em-dashes are fine in this CLAUDE.md and other prose docs.)

## What to do for a new design / module

1. Read `Architecture-Overview.md` for the conventions.
2. Read `proposed-file-structure.md` and the relevant module's design page (Vole's page is the best worked example).
3. If you're designing a brand-new module (Hare, Mole, Fox, Owl), use Vole's wiki page as a structural template: position-in-call-graph diagram, boundary table, internal structure, data model, public `gateway.py` API, the service class behind the gateway, wire surface, testing strategy, open questions.
4. Open or update the matching design issue (#1–#5).
