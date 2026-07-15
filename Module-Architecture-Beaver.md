Beaver is the **Widget Registry** module — the single source of truth for "what widgets exist in Canopy" and "how each one is configured." It serves widget metadata + JSON Schemas to four consumers (Screen Builder, the `.ui` converter, the runtime renderer, the legacy PyQt bridge) so all four agree on widget identity, prop shape, defaults, and Qt → React mapping.

Beaver is read-heavy and writes nothing through its API in v1. Widget definitions are authored as JSON files in a git-tracked directory and validated against a meta-schema at startup. If `canopy-api` restarts, Beaver re-reads every file, validates, and serves from an in-memory dict. Read latency is hashmap-lookup-fast.

> Status: this page is a *design proposal* matching the Canopy module conventions in `CLAUDE.md`. The pre-conventions handoff doc lives at `~/pydm_2/canopy-widget-registry-handoff.md` — this page is the conventions-aligned distillation. Open questions are concentrated in §13. Where this page contradicts the handoff doc, this page wins.

## 1. Position in the call graph

```
              ┌── palette + inspector schemas ──┐
              │                                  ▼
       Screen Builder ───────────────────────► Beaver ───► widgets/*.json (file system)
       .ui Converter ── by-qt-class lookup ──►   │
       Runtime Renderer ── validate props ────►  │
       Legacy PyQt Bridge ── /screens/*.ui ───►  │
       Wren ── action vocabulary check ────────► │
       Frontend ── HTTP + icon serving ───────►  │  /beaver/v1/...
```

- **Callers** (in-process, via `beaver.gateway`): Hare/Wren may consult Beaver to validate that a rule action target is in the supported per-widget vocabulary; the future converter may call `by_qt_class(...)` if it runs as a backend job; otherwise most callers are HTTP.
- **Callers** (HTTP, `/beaver/v1/...`): Screen Builder (palette + inspector schemas + PV picker is Raccoon, not Beaver), runtime renderer (`validate` on screen load), `.ui` converter CLI (`by-qt-class` per widget instance), legacy PyQt bridge (`/screens/{path}` static serving).
- **Callees**: the local file system. No network, no DB, no cache layer beyond the in-memory load.
- **Not callers** (deliberately): Beaver does *not* call Mole, Vole, Hare, Raccoon, or any directory service. Beaver is a metadata module; it has no opinions about live PV state.

Beaver publishes nothing on the event bus and subscribes to nothing.

## 2. Boundaries — what Beaver owns vs. delegates

| Concern | Beaver owns | Beaver delegates to |
|---|---|---|
| Widget identity (kebab-case `id`) | ✅ | — |
| Widget category, palette icon, display name | ✅ | — |
| Prop schema per widget (JSON Schema draft-07) | ✅ | — |
| Inspector grouping (Property Inspector field grouping) | ✅ | — |
| Defaults (initial geometry, prop defaults when dropped in SB) | ✅ | — |
| Qt → registry mapping (`qtMapping.class`, `qtPropMap`) | ✅ | — |
| Legacy `.ui` / `.py` static serving (PyQt bridge) | ✅ | — |
| Meta-schema validation at startup | ✅ | — |
| Validating a *widget instance config* against its schema | ✅ | — |
| Validating the *containing screen* against the IR | — | Screen IR (`canopy.contracts.screen` — see #screen-ir-issue) |
| Rule expression syntax / evaluation | — | Wren |
| PV name validation in `props.pv` | — | Raccoon (caller composes) |
| Icon authoring tools | — | (out of scope; SVGs are hand-authored in the registry repo) |
| Prop transform implementations (`stripProtocol`, `boolToFromPV`) | — | converter package — Beaver only names them in `qtPropMap` |

The discipline: **Beaver answers metadata questions about widget *types*, never about widget *instances at runtime* and never about anything outside the widget domain.** A composite "given this `.screen.json`, is every widget instance valid?" is the runtime renderer's job (which calls Beaver per widget node), not Beaver's.

## 3. Internal structure

The standard module shape; data tier is an in-memory dict populated from the file system at startup:

```
src/canopy/beaver/
  app.py                       # sub-FastAPI: v1 router + canopy error handler + lifespan
  runtime.py                   # uptime_s() for /health/watchdog
  api/v1/
    router.py                  # APIRouter(prefix="/v1")
    widgets.py                 # GET    /widgets
                               # GET    /widgets/{id}
                               # GET    /widgets/by-qt-class/{class}
                               # GET    /widgets/categories
                               # POST   /widgets/{id}/validate
    icons.py                   # GET    /icons/{filename}
    screens.py                 # GET    /screens
                               # GET    /screens/{path}        (legacy .ui/.py)
    health.py                  # GET    /health/{summary,watchdog}
  schemas/
    widget.py                  # WidgetSummaryDTO, WidgetDTO, CategoryDTO,
                               # ValidateRequestDTO, ValidateResultDTO
  services/
    widget_service.py          # WidgetService — orchestration over the catalog
  catalog/
    loader.py                  # FileCatalogLoader — read widgets/*.json, validate
                               # against the meta-schema, build the in-memory index
    meta_schema.py             # the JSON Schema for widget definition files
    index.py                   # WidgetIndex — by id, by qt-class, by category
  config.py                    # BeaverConfig — paths to widgets/, icons/, screens/
  gateway.py                   # public surface for other modules
  dependencies.py              # typed Depends aliases
```

The `catalog/` directory plays the role that `data/` plays in a DB-backed module. It is the data tier; nothing in `services/` or `api/` reaches past it to the file system without going through `WidgetIndex`.

## 4. Data model — the widget definition

The widget definition file is the only domain entity. One file per widget, in `widgets/{id}.json`. The shape (full reference in `~/pydm_2/canopy-widget-registry-handoff.md` §5):

```python
class WidgetDefinition(BaseModel):
    """Canonical shape of a widget definition file. Validated against the
    meta-schema at startup; serialized via WidgetDTO for the REST API."""

    id: str                              # kebab-case, also the filename stem
    version: str                         # semver; bumped on incompatible prop schema changes
    display_name: str
    description: str
    category: WidgetCategory             # indicators | controls | containers | plots | decorations | legacy
    palette_icon: str                    # filename under icons/

    react_component: ReactComponentRef   # name + module
    qt_mapping: QtMapping | None         # only for widgets with a Qt source
    prop_schema: dict                    # JSON Schema draft-07
    qt_prop_map: dict[str, QtPropMapping] | None
    inspector_schema: InspectorSchema    # field grouping for SB Property Inspector
    defaults: WidgetDefaults             # initial geometry / prop defaults
    supports_pv: bool
    supports_rules: bool
    supports_children: bool
```

`prop_schema` is opaque to Pydantic — it's a JSON Schema document that's validated structurally by the meta-schema and used at runtime by the `validate` endpoint. We do not try to map JSON Schema into Pydantic; the schema is data.

Two derived projections (DTOs) hit the wire:

- **`WidgetSummaryDTO`** — slim row for `GET /widgets` listings: `id`, `display_name`, `category`, `palette_icon` URL, `version`, `supports_pv`, `supports_rules`. No schema.
- **`WidgetDTO`** — the full definition for `GET /widgets/{id}` and `GET /widgets/by-qt-class/{class}`.

The seed list is `~/pydm_2/canopy-widget-seed-list.md`: P0 (8 widgets) ships first, P1 (12) second, P2 (15+) third. P0 unblocks SB MVP.

## 5. Storage strategy

### v1: file-based, in-memory index

```
widget-registry-data/            # external repo or subdir; configured via BeaverConfig.widgets_dir
├── widgets/                     # one JSON per widget
│   ├── pv-label.json
│   ├── pv-slider.json
│   └── ...
├── icons/                       # one SVG per widget
│   ├── label.svg
│   └── ...
├── legacy-screens/              # .ui / .py files served unchanged
│   └── ...
└── manifest.json                # generated at build: registry_version, widget index
```

The catalog repo is git-tracked and PR-reviewed. Beaver reads widgets/ at startup, validates each entry against the meta-schema, hashes the file, and builds three indexes (by id, by qt-class, by category). All reads are O(1) hashmap lookups.

**Why file-based.** Read-heavy workload (10ms in-memory lookups vs DB roundtrips), no migrations to manage, full git history per widget, contributors review changes via PR rather than running an admin UI. Squirrel-style Postgres backing is overkill until a UI-driven authoring flow appears.

### v2: hybrid (deferred)

Postgres-backed widget registry with file-based seed for migration. Add when (a) a widget admin UI lands, (b) hot-reload across replicas becomes a hard requirement, or (c) per-widget edit history beyond git is needed.

## 6. Multi-replica behavior

Each `canopy-api` replica reads the widget catalog from the same file system at startup. Widget definitions don't change at runtime in v1, so replicas don't need to coordinate. If a contributor merges a new widget JSON, the next deploy / restart picks it up.

A development-only file watcher (via `watchfiles`) reloads the index on file change so authoring is iterative without restart. Production startup-load is fine.

## 7. Lifecycle

**Startup.** Beaver exposes `beaver_lifespan()` (an `@asynccontextmanager` in `canopy.beaver.app`); `canopy.main.lifespan` enters it explicitly because mounted sub-apps do not run their own lifespan automatically. `beaver_lifespan` reads `BeaverConfig.widgets_dir`, walks every `*.json` file, validates each against the meta-schema, builds a `WidgetIndex`, and binds the `WidgetService` on the gateway via `gateway.bind(service)`. Startup fails loud if any widget file is malformed, an `id` collides with another widget, or `qt_mapping.class` collides with another widget. There is no partial-load mode; Beaver either has a clean catalog or refuses to start.

**Per-request.** All reads are in-memory hashmap lookups. The `validate` endpoint walks `prop_schema` against the request body via `jsonschema.validate(...)` synchronously (the schemas are tiny; no async needed).

**Health checks.** `/health/summary` returns `{ widget_count, category_count, registry_version, uptime_s }`. `/health/watchdog` is the standard 200/503. Beaver has no external dependencies; if the process is up, Beaver is healthy.

**Shutdown.** Nothing to clean up. The file watcher (dev only) is canceled.

## 8. Public API — `canopy.beaver.gateway`

The gateway is the only symbol other modules import from Beaver. Most consumers are HTTP, so the gateway surface is small.

```python
"""
canopy.beaver.gateway
======================

Public API for the Beaver widget-registry module.

Other Canopy modules import only from this file; the rest of `canopy.beaver.*`
is private implementation. The signatures here form the contract that survives
internal refactoring.
"""

from canopy.beaver.schemas.widget import (
    WidgetDTO,
    WidgetSummaryDTO,
    CategoryDTO,
    ValidateResultDTO,
)


async def get(widget_id: str) -> WidgetDTO:
    """Return the full widget definition for a kebab-case id.

    Raises:
        WidgetNotFound: no widget with this id is registered.
    """


async def by_qt_class(qt_class: str) -> WidgetDTO:
    """Return the widget definition whose `qt_mapping.class` matches.

    Used by the converter to translate a Qt class name to a registry id +
    prop-mapping spec.

    Raises:
        QtClassNotMapped: no widget has `qt_mapping.class == qt_class`.
            Caller (the converter) falls back to `unknown-widget`.
    """


async def list_summaries(category: str | None = None) -> list[WidgetSummaryDTO]:
    """Return slim row summaries, optionally filtered by category."""


async def categories() -> list[CategoryDTO]:
    """Return categories with widget counts."""


async def validate_props(
    widget_id: str, props: dict
) -> ValidateResultDTO:
    """Validate a props object against the widget's prop_schema.

    Returns ValidateResultDTO(valid=bool, errors=[{path, message}, ...]).

    Raises:
        WidgetNotFound: no widget with this id is registered.
    """
```

## 9. `WidgetService` — the implementation behind the gateway

Single in-memory implementation, constructed once at startup:

```python
class WidgetService:
    """Coordinates the widget index and validation; the implementation behind the gateway.

    Lifecycle: one instance per `canopy-api` process, constructed by
    `beaver_lifespan`. Tests can construct their own instance with a fake
    or fixture-loaded index and substitute it via `canopy.beaver.gateway.bind(service)`.
    """

    def __init__(self, index: WidgetIndex, config: BeaverConfig) -> None: ...

    async def get(self, widget_id: str) -> WidgetDTO: ...
    async def by_qt_class(self, qt_class: str) -> WidgetDTO: ...
    async def list_summaries(self, category: str | None) -> list[WidgetSummaryDTO]: ...
    async def categories(self) -> list[CategoryDTO]: ...
    async def validate_props(
        self, widget_id: str, props: dict
    ) -> ValidateResultDTO: ...
    async def stats(self) -> BeaverStatsDTO: ...
```

`WidgetIndex` is the data-tier object — three dicts (`by_id`, `by_qt_class`, `by_category`) and a `registry_version` derived from the manifest. It's immutable post-load in v1.

## 10. Wire surface — `/beaver/v1/...`

```
GET  /beaver/v1/widgets                        ?category=indicators       -> { widgets: [WidgetSummaryDTO], total }
GET  /beaver/v1/widgets/{id}                                               -> WidgetDTO
GET  /beaver/v1/widgets/by-qt-class/{class}                                -> WidgetDTO
GET  /beaver/v1/widgets/categories                                         -> { categories: [CategoryDTO] }
POST /beaver/v1/widgets/{id}/validate          body: { props: {...} }     -> ValidateResultDTO

GET  /beaver/v1/icons/{filename}                                           -> SVG (image/svg+xml, 1h cache)

GET  /beaver/v1/screens                                                    -> { screens: [LegacyScreenSummaryDTO] }
GET  /beaver/v1/screens/{path:path}                                        -> raw .ui / .py file

GET  /beaver/v1/health/summary
GET  /beaver/v1/health/watchdog
```

All query params and DTOs are camelCase on the wire (`displayName`, `paletteIcon`); the `CanopyModel` base's `to_camel` alias generator handles snake_case ↔ camelCase mapping. All endpoints gated by `Security(require_read_access)`. None of them require write access — Beaver is read-only in v1.

`validate` is POST (not GET) because the `props` body can be arbitrarily large.

Errors follow the project envelope `{code, detail}`:

| `CanopyError` subclass | `ErrorCode` slug | HTTP | When |
|---|---|---|---|
| `WidgetNotFound` | `beaver/widget_not_found` | 404 | unknown `id` on get / validate |
| `QtClassNotMapped` | `beaver/qt_class_not_mapped` | 404 | no widget has `qt_mapping.class == X` |
| `IconNotFound` | `beaver/icon_not_found` | 404 | unknown filename under `icons/` |
| `LegacyScreenNotFound` | `beaver/legacy_screen_not_found` | 404 | unknown `path` under `screens/` |
| `PropsInvalid` | `beaver/props_invalid` | 400 | request body is not a JSON object — note: schema-violations are returned in `ValidateResultDTO`, not raised; this slug is reserved for malformed request bodies |
| `CatalogMisconfigured` | `beaver/catalog_misconfigured` | 500 | startup-time meta-schema or duplicate-id failures that surface during a runtime reload |

## 11. Legacy `.ui` / `.py` serving

Beaver is the home for legacy file serving because it already has the file-system surface and the same consumers (Screen Builder, runtime renderer in fallback mode, future PyQt bridge) need both. The endpoints are deliberately thin:

- `GET /beaver/v1/screens` — lists every file under `BeaverConfig.legacy_screens_dir` with its relative path, byte size, and last-modified timestamp. No content parsing — Beaver does not understand `.ui` XML or PyQt code; it only serves bytes.
- `GET /beaver/v1/screens/{path}` — returns the file with `Content-Type: application/xml` for `.ui` and `text/x-python` for `.py`. `path` is validated to live under `legacy_screens_dir` (no traversal).

The PyQt bridge (out of scope for Beaver) consumes these endpoints to keep legacy `.ui`/`.py` screens running unchanged against the new backend.

## 12. Schema evolution

The widget definition file format itself is versioned via `meta_schema.version`. Bumping the meta-schema is a breaking change that requires migrating every widget JSON file in the catalog repo (a one-time PR). Per-widget `version` (semver) bumps when a widget's `prop_schema` changes incompatibly — runtime renderer / SB compare the saved screen's widget version against the current and either migrate or warn.

Migration helpers live in `canopy.contracts.screen` (see linked Screen IR issue), not Beaver — Beaver serves what's on disk; the IR layer is what knows about screen-level migrations.

## 13. Open architectural questions

1. **Codename.** "Beaver" matches the woodland theme and "builds things." Confirm with Yekta before the file `canopy/beaver/` lands. Alternatives if rejected: "Magpie" (collects shiny widgets), "Hedgehog" (curls around its data). Cannot collide with any existing module name.
2. **Port / sub-app mount.** `/beaver/v1/...` per Canopy convention. Internal port assignment (8005?) is a deployment detail.
3. **Catalog repo location.** Same monorepo as `canopy-api`, or a sibling repo (`canopy-widget-registry-data`)? Recommendation: **sibling repo** so widget contributors (frontend team, Tiffany) PR against a small scoped repo without pulling Canopy backend tooling. Beaver references the catalog via `BeaverConfig.widgets_dir` (path or git URL + ref).
4. **Icon strategy.** Inline SVG strings inside widget JSON, or separate `.svg` files served by `/beaver/v1/icons/{filename}`? Recommendation: **separate files**, browser-cacheable, easier to author with vector tools. Decided.
5. **Hot reload in production.** Skip for v1 (deploy-to-update is fine). If we add it later, the answer is a SIGHUP handler that re-runs the loader, swaps the index atomically, and rejects swap if the new catalog fails meta-schema validation. Not a NATS / WS push.
6. **`validate` vs ajv on the client.** The client (SB) already validates locally with ajv before save. The server-side `validate` endpoint is for the runtime renderer (which has no SB-baked validator) and as a final check for the converter. Both paths must agree — the meta-schema is shared. Decided.
7. **Coordinating prop-transform names with the converter.** `qt_prop_map.transform` is a string (e.g., `"stripProtocol"`). The converter package implements the function library; Beaver only names them. We need a published catalog of valid transform names so a widget JSON can't reference `"stripProtocl"` (typo). Recommendation: meta-schema includes an enum of allowed transform names, kept in sync with the converter package via CI. Tracking question for the converter-home issue.
8. **Should Wren consult Beaver for the rule action vocabulary?** D15 in the IR design locks the action vocabulary at the IR level (`visible`, `enabled`, `color`, ...). Per-widget `supports_rules` is already in the definition. Open question: is the *set of allowed action targets per widget* a Beaver concern (declared in the widget JSON) or a Wren concern (one global list)? Recommendation: **global list owned by Wren**, with `supports_rules: false` on widgets that opt out (e.g., `absolute-canvas`).

---

## Implementation phasing

Mapped to the seed list. Each phase ships an end-to-end demo against real Screen Builder + runtime renderer.

### Phase 1 — bones (1-2 days)

- `canopy.beaver` module skeleton: `app.py`, `gateway.py`, `dependencies.py`, `config.py`, `runtime.py`
- `BeaverConfig` reads `widgets_dir`, `icons_dir`, `legacy_screens_dir` from `canopy-config.yaml`
- `FileCatalogLoader` reads `*.json`, validates each against the meta-schema, builds `WidgetIndex`
- `WidgetService` with `get`, `by_qt_class`, `list_summaries`, `categories`
- `GET /beaver/v1/widgets`, `/widgets/{id}`, `/widgets/by-qt-class/{class}`, `/widgets/categories`
- `/health/{summary,watchdog}`
- 3 P0 widget JSON files for testing: `pv-label`, `pv-slider`, `pv-led`
- Test the gateway (`bind(service)` pattern matching Mole/Vole/Raccoon)

**Demo:** `curl /beaver/v1/widgets` returns three widgets; SB palette renders them.

### Phase 2 — P0 catalog complete (2-3 days)

- Author the remaining 5 P0 widgets: `absolute-canvas`, `pv-text-input`, `pv-button`, `pv-enum-combobox`, `embedded-display`
- `qt_prop_map` populated from `~/pydm_2/pydm-converter-plan.md` lines 172-376
- `inspector_schema` populated using Tiffany's mapping for variants (Carbon Button → primary/secondary/...)
- Icon SVGs (8 files)
- `POST /beaver/v1/widgets/{id}/validate`
- `GET /beaver/v1/icons/{filename}` (SVG with cache headers)

**Demo:** SB palette renders all 8 P0 widgets; dragging `pv-label` populates the inspector with a working schema-driven form.

### Phase 3 — legacy `.ui` / `.py` serving (1 day)

- `GET /beaver/v1/screens` directory walk
- `GET /beaver/v1/screens/{path:path}` with traversal guard

**Demo:** PyQt bridge (when it exists) loads a known PyDM `.ui` from `/beaver/v1/screens/vacuum.ui` unchanged.

### Phase 4 — P1 catalog (3-5 days)

- 12 P1 widget JSON files (`pv-checkbox`, `pv-byte-led`, `pv-progress-bar`, `pv-radio-group`, `text-label`, `regular-button`, `group-box`, `tabs`, `frame`, `scroll-area`, `image-view`, `related-display-button`)
- Coordinate with converter on prop-transform names

### Phase 5 — hardening & handoff (2 days)

- Dev-only file watcher for hot reload
- Prometheus metrics (request count, validate count, validate-failure count)
- README in the catalog repo documenting how to author a new widget definition
- Pair with Screen Builder owner: SB calls Beaver, no surprises
- Pair with converter owner: `by_qt_class` covers every PyDM widget the converter currently handles; transform names align

### Out of v1 (deferred)

- Postgres backing (Phase 2 in handoff doc)
- Hot reload across replicas / SIGHUP
- MCP server
- Widget admin UI
- Custom widget plugin upload
- Per-widget edit history beyond git

---

## Acceptance for v1

A reviewable v1 must:

- [ ] Mount as `/beaver/v1/...` under `canopy-api`, with health endpoints
- [ ] Pass meta-schema validation on startup; fail loud on malformed widget files or duplicate `id` / `qt_mapping.class`
- [ ] Serve all 8 P0 widget definitions from the seed list
- [ ] Return correct schemas through every documented endpoint
- [ ] Serve at least one round-tripped `.ui` file via `/screens/{path}`
- [ ] Validate a sample widget config via `POST /widgets/{id}/validate` — both pass and fail cases
- [ ] OpenAPI docs auto-generated and accurate
- [ ] Errors follow `{code, detail}` envelope with `beaver/*` slugs
- [ ] DTOs serialize as camelCase on the wire and accept either form on input
- [ ] Unit tests for the loader, meta-schema, gateway, validate path
- [ ] One integration test that boots the module and exercises every endpoint
- [ ] Pair-tested against the Screen Builder palette and inspector
