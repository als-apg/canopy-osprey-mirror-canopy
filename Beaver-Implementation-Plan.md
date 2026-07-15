Implementation plan for the Widget Registry module (issue #16). Pairs with `Module-Architecture-Beaver.md` — that doc covers the *what*; this one covers the *how*. Branch: `yektay/#16-beaver-widget-registry`.

> Tentative codename **Beaver**; every file path below assumes it. If the codename changes, mass-rename `beaver` → `<new>` and re-issue.

---

## 0. Decisions to lock before code lands

Each affects file layout or wiring. Locking now avoids rework.

| # | Decision | Default if not raised | Reversibility |
|---|---|---|---|
| 1 | Module codename | `beaver` | Cheap rename; do early |
| 2 | Catalog location | in-tree subdir `widget-registry-data/` (sibling repo a v2 option) | Path is a config knob; cheap to move |
| 3 | Path under canopy-api | `/beaver/v1/...` | Per Canopy convention; not negotiable |
| 4 | Auth | `require_read_access` everywhere; no write endpoints in v1 | Already locked by API conventions |
| 5 | Catalog source-of-truth format | one JSON file per widget under `widgets/` | Per `~/pydm_2/canopy-widget-registry-handoff.md` §6 |

---

## 1. Module shape — file map

Mirrors Hare exactly (see `src/canopy/hare/`). New files under `src/canopy/beaver/`:

```
src/canopy/beaver/
  __init__.py                  # empty
  app.py                       # FastAPI sub-app + beaver_lifespan asynccontextmanager
  runtime.py                   # mark_started() / uptime_s()  (copy of hare/runtime.py)
  config.py                    # BeaverConfig (pydantic-settings, CANOPY_BEAVER__ env)
  errors.py                    # BeaverError + WidgetNotFound / QtClassNotMapped / etc.
  gateway.py                   # bind() / get_service() / public async functions
  dependencies.py              # WidgetServiceDep typed Depends alias
  api/
    __init__.py
    v1/
      __init__.py
      router.py                # APIRouter(prefix="/v1") composes sub-routers
      health.py                # /health/{watchdog,summary}
      widgets.py               # /widgets/* (list, get, by-qt-class, categories, validate)
      icons.py                 # /icons/{filename}
      screens.py               # /screens (list) + /screens/{path:path}
  schemas/
    __init__.py
    health.py                  # HeartbeatDTO, HealthSummaryDTO (Beaver-shaped)
    widget.py                  # WidgetSummaryDTO, WidgetDTO, CategoryDTO,
                               # ValidateRequestDTO, ValidateResultDTO, ReactComponentRef,
                               # QtMapping, QtPropMapping, InspectorSchema, WidgetDefaults
  services/
    __init__.py
    widget_service.py          # WidgetService — orchestration
  catalog/
    __init__.py
    loader.py                  # FileCatalogLoader.load() -> WidgetIndex
    meta_schema.py             # META_SCHEMA (JSON Schema for the widget definition file)
    index.py                   # WidgetIndex with by_id / by_qt_class / by_category
```

Tests under `tests/canopy/beaver/`:

```
tests/canopy/beaver/
  __init__.py
  conftest.py                  # tmp_catalog fixture, beaver_config fixture, client fixture
  test_loader.py               # meta-schema valid/invalid/duplicate-id/duplicate-qt-class
  test_widget_index.py         # by-id, by-qt-class, by-category
  test_widget_service.py       # gateway-level ops + validate_props
  test_widgets_endpoint.py     # /widgets, /widgets/{id}, /widgets/by-qt-class
  test_validate_endpoint.py    # /widgets/{id}/validate pass + fail
  test_categories_endpoint.py
  test_icons_endpoint.py
  test_screens_endpoint.py
  test_health.py
```

Catalog data under repo root (in-tree v1):

```
widget-registry-data/
  widgets/                     # one .json per widget
    pv-label.json              # P0
    pv-slider.json             # P0
    pv-led.json                # P0
    pv-text-input.json         # P0
    pv-button.json             # P0
    pv-enum-combobox.json      # P0
    embedded-display.json      # P0
    absolute-canvas.json       # P0
    ...                        # P1 / P2 added later
  icons/                       # one .svg per widget
    pv-label.svg
    ...
  legacy-screens/              # serve raw to PyQt bridge
  manifest.json                # generated; { registry_version, widget_count }
```

---

## 2. Wiring in `canopy/main.py`

Three edits, modeled exactly on the existing Hare/Mole composition:

```python
# imports
from canopy.beaver.app import app as beaver_app
from canopy.beaver.app import beaver_lifespan

# inside lifespan asynccontextmanager
async with mole_lifespan(), hare_lifespan(), beaver_lifespan():
    ...

# mount
app.mount("/beaver", beaver_app)
```

No other changes to `main.py`. Beaver registers its own `register_canopy_error_handler(app)` inside `app.py` (same pattern Hare uses).

---

## 3. Error codes — addition to `canopy/core/errors.py`

Append new slugs to `ErrorCode` (auto-derived from member name):

```python
# Beaver
WIDGET_NOT_FOUND = auto()
QT_CLASS_NOT_MAPPED = auto()
ICON_NOT_FOUND = auto()
LEGACY_SCREEN_NOT_FOUND = auto()
PROPS_INVALID = auto()
CATALOG_MISCONFIGURED = auto()
```

Beaver's own `errors.py` defines the exception subclasses (mirrors `hare/errors.py`):

```python
from canopy.core.errors import CanopyError, ErrorCode


class BeaverError(CanopyError):
    """Base for all Beaver errors."""


class WidgetNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.WIDGET_NOT_FOUND


class QtClassNotMapped(BeaverError):
    status_code = 404
    error_code = ErrorCode.QT_CLASS_NOT_MAPPED


class IconNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.ICON_NOT_FOUND


class LegacyScreenNotFound(BeaverError):
    status_code = 404
    error_code = ErrorCode.LEGACY_SCREEN_NOT_FOUND


class PropsInvalid(BeaverError):
    status_code = 400
    error_code = ErrorCode.PROPS_INVALID


class CatalogMisconfigured(BeaverError):
    status_code = 500
    error_code = ErrorCode.CATALOG_MISCONFIGURED
```

Schema-violations from `validate_props` are NOT raised — they are returned in `ValidateResultDTO(valid=False, errors=[...])`. `PropsInvalid` is reserved for malformed request bodies (not a JSON object).

---

## 4. Configuration — `beaver/config.py`

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class BeaverConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_BEAVER__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    widgets_dir: Path = Path("widget-registry-data/widgets")
    icons_dir: Path = Path("widget-registry-data/icons")
    legacy_screens_dir: Path = Path("widget-registry-data/legacy-screens")
    icon_cache_max_age_s: int = 3600


def get_beaver_config() -> BeaverConfig:
    return BeaverConfig()
```

Sibling-repo deploy: set `CANOPY_BEAVER__WIDGETS_DIR=/srv/canopy/registry/widgets` etc. in production env.

---

## 5. Lifespan — `beaver/app.py`

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.beaver import gateway
from canopy.beaver.api.v1.router import router as v1_router
from canopy.beaver.catalog.loader import FileCatalogLoader
from canopy.beaver.config import get_beaver_config
from canopy.beaver.runtime import mark_started
from canopy.beaver.services.widget_service import WidgetService
from canopy.core.errors import register_canopy_error_handler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def beaver_lifespan():
    """Load the widget catalog, validate against meta-schema, bind the service."""
    config = get_beaver_config()
    loader = FileCatalogLoader(config)
    index = loader.load()  # raises CatalogMisconfigured on any bad file
    service = WidgetService(index, config)
    gateway.bind(service)
    mark_started()
    logger.info(
        "Beaver started: %d widgets, %d categories, registry %s",
        index.widget_count,
        index.category_count,
        index.registry_version,
    )
    try:
        yield
    finally:
        gateway.bind(None)
        logger.info("Beaver shutdown complete")


app = FastAPI(
    title="Canopy / Beaver",
    description="Widget registry — metadata + JSON Schemas for Canopy widgets.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
```

---

## 6. Catalog loader — `beaver/catalog/loader.py`

Two responsibilities: validate every file against the meta-schema; build the three indexes. Synchronous (file system + jsonschema), called once at startup.

Pseudocode:

```python
class FileCatalogLoader:
    def __init__(self, config: BeaverConfig) -> None: ...

    def load(self) -> WidgetIndex:
        # walk widgets_dir for *.json
        # for each: read, parse, validate against META_SCHEMA
        # collect: by_id, by_qt_class, by_category
        # detect collisions: id, qt_mapping.class
        # compute registry_version from a sha256 of sorted (id, version) pairs
        # raise CatalogMisconfigured on any failure (with per-file path)
        return WidgetIndex(...)
```

Failures during load propagate as `CatalogMisconfigured` with a useful message; `canopy-api` refuses to start. This is intentional — a half-loaded registry is worse than no registry.

---

## 7. Wire surface — endpoints

Concrete routes, all under `/beaver/v1`:

| Method | Path | Auth | Handler |
|---|---|---|---|
| GET | `/widgets` | read | `widgets.list_widgets(category=None)` |
| GET | `/widgets/{id}` | read | `widgets.get_widget` |
| GET | `/widgets/by-qt-class/{qt_class}` | read | `widgets.by_qt_class` |
| GET | `/widgets/categories` | read | `widgets.list_categories` |
| POST | `/widgets/{id}/validate` | read | `widgets.validate_widget` |
| GET | `/icons/{filename}` | read | `icons.get_icon` (`Content-Type: image/svg+xml`, cache header) |
| GET | `/screens` | read | `screens.list_legacy_screens` |
| GET | `/screens/{path:path}` | read | `screens.get_legacy_screen` (traversal-guarded) |
| GET | `/health/watchdog` | none | `health.watchdog` |
| GET | `/health/summary` | read | `health.summary` |

All routes include `Security(require_read_access)` per `core/security.py`. Watchdog has no auth (parity with Hare).

---

## 8. Dependencies — `pyproject.toml`

Add to `[project] dependencies`:

```toml
"jsonschema>=4.21,<5",      # prop_schema validation in WidgetService.validate_props
```

Optionally add to dev-dependencies for hot-reload (Phase 5):

```toml
"watchfiles>=0.21,<1",
```

`uv lock` after editing.

---

## 9. Tests — what each file covers

| File | Covers |
|---|---|
| `conftest.py` | `tmp_catalog` builds a temp dir with N widget JSONs; `client` builds a `TestClient` against a Beaver app whose lifespan is bound to that tmp catalog |
| `test_loader.py` | (a) all P0 widgets load; (b) malformed widget JSON raises `CatalogMisconfigured`; (c) duplicate `id` raises; (d) duplicate `qt_mapping.class` raises; (e) missing required field raises; (f) registry_version is deterministic |
| `test_widget_index.py` | by-id, by-qt-class, by-category lookups; missing keys raise the right errors |
| `test_widget_service.py` | `get`, `by_qt_class`, `list_summaries`, `categories`, `validate_props` happy + sad paths; gateway `bind`/`get_service` round-trip |
| `test_widgets_endpoint.py` | every route + camelCase wire serialization + 404 envelope; query params (`?category=indicators`) |
| `test_validate_endpoint.py` | pass case (returns `{valid: true}`); fail case (returns `{valid: false, errors: [...]}` with JSON Pointer-ish paths); malformed body raises `PropsInvalid` |
| `test_categories_endpoint.py` | counts match the seed catalog |
| `test_icons_endpoint.py` | served SVG, content-type, cache header, traversal denied |
| `test_screens_endpoint.py` | listing + content + traversal denied |
| `test_health.py` | watchdog (no auth) + summary (auth + counts) |

Use real catalog files in tests (small fixtures), not mocks. The loader is fast on tiny inputs and the integration value is high.

---

## 10. Phasing (mapped to design doc)

| Phase | Scope | Output | Approx. size |
|---|---|---|---|
| 1 | Bones: module skeleton, `BeaverConfig`, loader, meta-schema, `WidgetService` (`get`, `by_qt_class`, `list_summaries`, `categories`); 3 P0 widgets (`pv-label`, `pv-slider`, `pv-led`); GETs and health | First mergeable PR; demo: `curl /beaver/v1/widgets` returns 3 | 1-2 d |
| 2 | P0 catalog complete: remaining 5 P0 widgets; `validate` endpoint; icons; `qt_prop_map` populated from `pydm-converter-plan.md` lines 172-376 | SB palette renders 8 widgets and inspector form works | 2-3 d |
| 3 | Legacy screen serving | PyQt bridge can fetch a known `.ui` | 1 d |
| 4 | P1 catalog (12 widgets) | SB palette covers most operator-screen needs | 3-5 d |
| 5 | Hardening: dev file watcher, Prometheus metrics, README in catalog repo, pair-test with SB and converter | v1 ready for sign-off | 2 d |

---

## 11. Acceptance for the Phase 1 PR

- [ ] `/Users/yazar/projects/Canopy/src/canopy/beaver/` exists with the file map above
- [ ] `canopy-api` boots cleanly with Beaver mounted at `/beaver`
- [ ] `GET /beaver/v1/widgets` returns 3 widgets (camelCase wire)
- [ ] `GET /beaver/v1/widgets/pv-label` returns the full WidgetDTO
- [ ] `GET /beaver/v1/widgets/by-qt-class/PyDMLabel` returns pv-label
- [ ] `GET /beaver/v1/widgets/categories` returns category rollup
- [ ] `GET /beaver/v1/health/watchdog` and `/health/summary` work
- [ ] All 4xx/5xx responses use the `{code, detail}` envelope
- [ ] Startup fails loud on a malformed widget JSON (with the file path in the message)
- [ ] Tests cover loader, service, and route layers; pre-commit hooks pass
- [ ] No tracked changes outside `src/canopy/beaver/`, `src/canopy/core/errors.py`, `src/canopy/main.py`, `tests/canopy/beaver/`, `widget-registry-data/`, `pyproject.toml`, `uv.lock`

---

## 12. Followups out of scope for the Phase 1 PR

- Validate endpoint + icons + legacy serving (Phase 2-3)
- File watcher (Phase 5)
- Prometheus metrics (Phase 5)
- Cross-cutting prop-transform name catalog (issue #18 territory — converter/TSX home)
- Wren rule-action vocabulary alignment (open question §13.8 in the design doc)
- README in the catalog repo (Phase 5 / handoff)
