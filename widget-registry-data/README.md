# Canopy widget catalog

Source-of-truth files for the **Beaver** widget-registry service (`canopy.beaver`). Each widget is one JSON document under `widgets/`; each icon is one SVG under `icons/`; legacy PyDM screens for the PyQt compatibility bridge live under `legacy-screens/`.

Beaver loads this directory at startup, validates every widget against the meta-schema in `src/canopy/beaver/catalog/meta_schema.py`, builds three indexes (by id, by Qt class, by category), and serves them over `/beaver/v1/widgets`. Failures during load are fatal — `canopy-api` refuses to start with a malformed catalog.

## Adding a widget

1. Create `widgets/<id>.json` with the schema below.
2. Create `icons/<id>.svg` (24×24 viewBox, prefer `fill="none" stroke="currentColor"` so themes can color them).
3. Run the loader tests: `uv run pytest tests/canopy/beaver/test_loader.py -v`.
4. Run the health test (boots the real catalog): `uv run pytest tests/canopy/beaver/test_health.py`.
5. Open a PR.

## Widget JSON schema

Required top-level fields (camelCase on the wire; Pydantic accepts either form on input):

| Field | Type | Notes |
|---|---|---|
| `id` | kebab-case string | Also the filename stem. Must match `^[a-z][a-z0-9-]*$`. |
| `version` | semver string | Bump on incompatible `propSchema` changes. |
| `displayName` | string | Shown in the SB palette. |
| `description` | string | Tooltip in SB. |
| `category` | enum | One of `indicators`, `controls`, `containers`, `plots`, `decorations`, `legacy`. |
| `paletteIcon` | filename | Must exist under `icons/`. |
| `reactComponent` | `{name, module}` | Drives the TSX-emitter imports. |
| `propSchema` | JSON Schema (draft-07) | Validates a widget instance's `props` block. |
| `inspectorSchema` | `{groups: [{name, fields}]}` | Field grouping in the SB Property Inspector. |
| `supportsPv` | boolean | Widget binds to a PV. |
| `supportsRules` | boolean | Wren rules can target this widget. |
| `supportsChildren` | boolean | True for containers (`group-box`, `tabs`, `frame`, `scroll-area`, `absolute-canvas`). |

Optional:

| Field | Notes |
|---|---|
| `qtMapping` | `{class, extends?, header?}` — present only if the widget maps from a `.ui` Qt class. Omit for SB-native widgets. |
| `qtPropMap` | `{<qtPropName>: {to, transform?}}` — converter prop translation. `transform` is a name from the converter's transform library (see below). |
| `defaults` | `{width?, height?}` — initial geometry when dropped in SB. |

## Naming and ID conventions

- IDs are kebab-case, lowercase, alphanumerics + dashes: `pv-label`, `pv-byte-led`, `embedded-display`.
- React component names are PascalCase: `PVLabel`, `PVByteLed`.
- Filename stems must equal the `id`. The loader does not enforce this, but tooling and reviewers do.
- Two widgets MUST NOT share the same `id` or the same `qtMapping.class`. The loader rejects either with a `CatalogMisconfigured` startup error naming both files.

## qtMapping decisions

- For widgets that have both a PyDM-prefixed and a vanilla Qt class (e.g. `PyDMLabel` / `QLabel`, `PyDMPushButton` / `QPushButton`), register the **PyDM class** on the PV-bound widget and the **vanilla class** on the non-PV widget. They do not collide and the converter dispatches correctly: `pv-label` ← `PyDMLabel`, `text-label` ← `QLabel`.
- For widgets with no clean Qt source (e.g. `pv-radio-group` — clusters of `QRadioButton` need group-detection the converter does not do), omit `qtMapping` entirely. The converter falls through to the `unknown-widget` placeholder for those screens.

## Prop transform names

The `qtPropMap.<prop>.transform` field names a function in the converter's transform library. Currently in use:

| Name | Purpose |
|---|---|
| `stripProtocol` | Strip `ca://` / `pva://` from PV channel strings. |
| `boolToFromPV` | Map `precisionFromPV: true` → `precision: "fromPV"`. |
| `qtOrientation` | Qt `Qt::Horizontal` / `Qt::Vertical` → `"horizontal"` / `"vertical"`. |
| `qtAlignment` | Qt alignment flags → `"left"` / `"center"` / `"right"`. |
| `qtFrameShape` | Qt frame shape enum → kebab-case enum. |
| `qtFrameShadow` | Qt frame shadow enum → `"plain"` / `"raised"` / `"sunken"`. |
| `qtScrollPolicy` | Qt scrollbar policy enum → `"auto"` / `"always-on"` / `"always-off"`. |
| `firstOf` | Take the first item from a list (e.g., `filenames` → `file`). |

Adding a new transform name requires a corresponding implementation in the converter package — coordinate via the converter / TSX home issue (#18).

## Icons

24×24 SVGs. Stroke-only (`fill="none" stroke="currentColor"`) is preferred so the SB palette can re-color them via CSS. Stubs in this repo are placeholder geometry; designers replace them later. The icons endpoint serves with `Cache-Control: public, max-age=3600` (configurable via `CANOPY_BEAVER__ICON_CACHE_MAX_AGE_S`); changes need a registry-version bump or a cache-buster to take effect on warm clients.

## Legacy screens

`legacy-screens/` holds untouched `.ui` and `.py` files that the PyQt compatibility bridge fetches via `GET /beaver/v1/screens/{path}`. Beaver does not parse them — it serves bytes. Drop a file here, run `pytest tests/canopy/beaver/test_screens_endpoint.py`, and it is served.

## Local validation

```sh
# Loader tests (validates your widget JSON against the meta-schema).
uv run pytest tests/canopy/beaver/test_loader.py -v

# Boots the parent app and exercises /beaver/v1/health/summary.
uv run pytest tests/canopy/beaver/test_health.py

# Full Beaver test suite.
uv run pytest tests/canopy/beaver/
```

The loader fails loud on any malformed widget JSON, duplicate `id`, or duplicate `qtMapping.class`. The error message names the offending file path.

## What this catalog is not

- **Not a runtime config service.** There is no live mutation API. Changes go through PR + redeploy.
- **Not a render engine.** Beaver returns metadata and JSON Schemas; the React renderer in `@canopy/widgets` instantiates the actual components.
- **Not a PV directory.** Channel autocomplete is `Raccoon`'s job.
