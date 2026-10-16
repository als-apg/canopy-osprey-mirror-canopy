# canopy_mcp

The panel-builder MCP server. It ships in the canopy wheel as a second
top-level package, installed with the `mcp` extra, and talks to the canopy
backend over HTTP only.

## Install

```sh
uv sync --extra mcp
uv run playwright install --with-deps chromium
```

The second command is needed only for the `screenshot` tool, which renders
screens in a headless Chromium. `--with-deps` also installs the system
libraries Chromium needs on Linux. On a developer Mac,
`uv run playwright install chromium` is enough.

The browser is lazy. Nothing launches at startup: the first `screenshot` call
launches Chromium, and the server stops it again 300 s after the last capture.
A server without Playwright or without Chromium still starts. Its `screenshot`
tool then answers that screenshots are unavailable, and every other tool
works.

## Run

```sh
CANOPY_VIEWER_DIST=/path/to/viewer/dist python -m canopy_mcp --http --host 127.0.0.1 --port 8765
```

Only the streamable-HTTP transport is supported. The same port serves the MCP
endpoint (`/mcp`), the embedded viewer (`/embed/...`), the draft screens
(`/drafts/...`) and the saved views (`/views`).

`--fd N` serves on a listening socket the parent process already holds, passed
down as descriptor `N`, instead of binding `--host` and `--port`. A supervisor
that keeps the socket across restarts loses no connection attempt while the
server is down.

## Switches

The server reads its settings from the environment. An empty value counts as
unset.

| Variable | Default | Meaning |
|---|---|---|
| `CANOPY_BASE_URL` | `http://127.0.0.1:8000` | Where the canopy backend is reachable. |
| `CANOPY_API_KEY_FILE` | unset | File holding the read-scope key sent to the backend. When set, `CANOPY_API_KEY` is ignored. |
| `CANOPY_ACCESS_TOKEN_FILE` | unset | File holding the credential every request except `GET /health` must carry as `X-Canopy-Access`. Unset means no credential is asked for. |
| `CANOPY_SELF_URL` | loopback on the served port | The address the screenshot browser reaches this server at. |
| `CANOPY_DRAFTS_DIR` | `drafts` | The store the tools write to. Screens are `<dir>/screens/<id>.screen.json`. |
| `CANOPY_LIBRARY_DIR` | unset | A read-only screen library served beside the drafts. Its screens are `library/<id>` to the tools and `/embed/library/<id>` to the viewer. Only its `screens/` subtree is served. |
| `CANOPY_AUTHORING` | `1` | `0` takes the seven authoring tools off the server. |
| `CANOPY_PUSH_DIR` | unset | Where `request_push` writes its bundles, one directory per request. Unset means the tool answers `push_unavailable`. |
| `CANOPY_VIEWS_DIR` | `<CANOPY_DRAFTS_DIR>/views` | Where the saved views are stored. |
| `CANOPY_COLLECTIONS_DIR` | `<CANOPY_DRAFTS_DIR>/collections` | Where the user's own collections and their selection state are stored. |
| `CANOPY_FACILITY_COLLECTIONS_DIR` | unset | A read-only directory of facility collections shipped with the deployment. Unset means no facility collections. |
| `CANOPY_PV_MODE` | `hare` | `mock` makes the served viewer draw PVs from the mock service instead of the backend. |
| `CANOPY_VIEWER_DIST` | unset | The built viewer. Its `ir-map.json` is the widget catalog. |
| `CANOPY_SPEC_DIR` | `spec/` beside `CANOPY_VIEWER_DIST` | The staged specification: the Screen-IR schemas and the per-widget prop schemas `check` validates against. Without it `check` returns one `spec_unavailable` warning in place of the schema and prop findings, and `list_widgets`, `get_widget` and `get_schema` carry a `spec_unavailable` note. |
| `CANOPY_BROWSER_SANDBOX` | `0` | `1` launches the screenshot Chromium with its own sandbox. Off by default, because the sandbox needs privileges a container usually lacks. If Chromium cannot start with it, `screenshot` answers unavailable and names this switch. |
| `CANOPY_HOST_STYLESHEETS` | unset | The embedding host's stylesheets: whitespace-separated relative `.css` paths, resolved under the proxy prefix. See *Host stylesheets* below. An entry that is not a plain relative `.css` path stops the server at startup. |

A secret file that is named but cannot supply its secret ends the launch with
a non-zero status before any socket is bound.

## Host stylesheets

A host that embeds the panel behind a proxy can restyle it with its own
stylesheets. With `CANOPY_HOST_STYLESHEETS` set, every entry page (`/`,
`/s/*`, `/embed/*`, `/trace/`, `/trace/correlation.html`,
`/trace/waveform.html`) requested with a
valid `X-Forwarded-Prefix` carries one

```html
<link rel="stylesheet" data-canopy-host-sheet href="<prefix>/<path>">
```

per entry, in the order given, as the last children of `<head>`, and
`<html>` gains `data-canopy-host`. The hrefs are absolute under the prefix, so
the host serves the files, not this server.

The prefix decides both whether anything is emitted and what the hrefs are. A
request without the header, or with a malformed one, gets the page unchanged.
That includes this server's own screenshot browser and loopback health
probes, which reach it directly: they render the standalone look by design
and never request a host file. No script is added to any page and no URL
parameter reaches the list.

## Tools

The server offers 17 tools. Ten only read. Seven are authoring tools: they
create, change or delete drafts, or file a draft for the shared library. With
`CANOPY_AUTHORING=0` the server offers the ten.

| Tool | Kind | What it does |
|---|---|---|
| `list_widgets` | read | Lists the widget types a screen may use. |
| `get_widget` | read | Describes one widget type and its props. |
| `get_schema` | read | Returns the Screen-IR JSON Schema. |
| `check` | read | Reports schema, semantic, ref and lint findings for a document or a stored screen. |
| `get_screen` | read | Returns one screen and its rev. |
| `list_drafts` | read | Lists the drafts, then the library screens. |
| `search_pvs` | read | Searches the PV directory by a glob pattern. |
| `read_pv` | read | Reads one PV once. |
| `screenshot` | read | Renders a screen as a PNG. |
| `list_collections` | read | Lists the collections the viewer offers, with each member checked against this host. |
| `new_screen` | authoring | Saves a whole document as a draft. |
| `patch_node` | authoring | Changes one node by JSON Merge Patch. |
| `add_node` | authoring | Appends one node to a container. |
| `remove_node` | authoring | Removes one node and its children. |
| `delete_draft` | authoring | Deletes a draft. |
| `convert_legacy` | authoring | Converts a `.bob`, `.opi`, `.edl` or `.ui` panel and its sub-screens into drafts. |
| `request_push` | authoring | Files a draft and everything it refers to as a bundle for an administrator to review. Nothing is published by the call. |

No tool writes a PV. Every write to a screen is checked against the rev the
caller last saw, so an agent and a person editing in the viewer cannot
overwrite each other. A library screen (`library/<id>`) can be read, checked
and screenshotted. An authoring tool refuses it with `read_only_store`.

## The skill

`skill/SKILL.md` tells an agent how to use the tools. It is the single source
of that text and ships in the wheel as package data. Install it into a project
with:

```sh
python -m canopy_mcp.skill install <dir>
```

This writes the same bytes to
`<dir>/.claude/skills/canopy-panel-builder/SKILL.md` and prints the path.
Running it again is safe. The server's MCP `instructions` point at the skill.

## The report

```sh
python -m canopy_mcp.report <root> [--served-at personal|library] (--emit|--write|--check)
```

`<root>` is a store root, the directory that holds `screens/`. The report
lists what the screens of the tree can write, run, open and load. `--write`
puts it beside `screens/` as `WRITE-BINDINGS.json` and `WRITE-BINDINGS.md`,
`--check` recomputes it and exits non-zero on a mismatch or a refusal, and
`--emit` prints it without writing. Run `--write` before committing a screen
tree that carries a report.

## Saved views

A saved view is a named Trace query. The viewer finds the API through
`config.json` (`"views": {"path": "views"}`). Each view is stored as
`<CANOPY_DRAFTS_DIR>/views/<id>.json`, next to `screens/` rather than inside
it, so views never show up in the drafts manifest or under `/drafts`.
`CANOPY_VIEWS_DIR` names another directory for them.

| Request | Answer |
|---|---|
| `GET /views` | 200 `{"views": [view, ...]}`, sorted by name (case-insensitive), then id |
| `GET /views/{id}` | 200 view, 404 missing, 400 invalid id |
| `PUT /views/{id}` | 201 created or 200 replaced, with the stored view as the body |
| `DELETE /views/{id}` | 204, 404 missing, 400 invalid id |

A view is `{"id", "name", "query", "updated"}`, where `updated` is an ISO-8601
UTC timestamp. The id must match `^[A-Za-z0-9_-]{1,64}$`. The `PUT` body is
exactly `{"name": str, "query": str}`. The name is stripped and must then be 1
to 120 characters with no control characters. The query is the Trace embed
query, for example `pv=A:B&range=-1h`, which may also carry Trace's axis and
display settings as `plot=<JSON>`. The server stores it as an opaque string;
it may be empty and is limited to 4096 UTF-8 bytes. A `PUT` answers 415 unless `Content-Type` is
`application/json` and 413 when the body is larger than 16 KiB. Any other
invalid body is a 400. Error bodies are `{"error": "..."}`.

Every response carries `Cache-Control: no-cache`. The routes are reachable
through the OSPREY proxy in the same way as the drafts and carry no auth of
their own. As a CSRF guard, `PUT` and `DELETE` answer 403 when a
`Sec-Fetch-Site` header is present and is neither `same-origin` nor `none`.
Writes are atomic (temp file plus `os.replace`). A views directory that cannot
be written answers `PUT` and `DELETE` with 403 and the code `read_only_store`.

## Collections

A collection is a named, ordered set of screens and tools the viewer opens
together, optionally with a dock layout. The viewer finds the API through
`config.json` (`"collections": {"path": "collections"}`). Two directories feed
it:

- the user's own collections, `<CANOPY_DRAFTS_DIR>/collections/<id>.json`
  (or `CANOPY_COLLECTIONS_DIR`), written through `PUT` and `DELETE
  /collections/{id}`, beside the per-user `.state.json` that records which
  collections are ticked, the pinned members and the recently opened ones;
- the facility collections, `CANOPY_FACILITY_COLLECTIONS_DIR/<id>.json`, which
  the server only reads. A file that breaks a rule is logged once and skipped.

`GET /collections` lists the facility collections first, then the personal
ones. The file format, the rules and the routes are described in the
[collections author page](../../docs/how_to/collections.md) and in the
docstring of `canopy_mcp.collections`, which is the one specification.
`python -m canopy_mcp.collections <dir> --check [--library <root>]` checks a
facility directory before it is deployed.

## Screenshots

`screenshot(id, macros, theme)` loads this server's own
`/embed/drafts/<id>?<macros>`, or `/embed/library/<id>` for a library screen,
in the pv mode the server is configured for (`CANOPY_PV_MODE`). `theme` is
`light` or `dark`; left out, the viewer's default applies. It waits up to
4 s for web fonts and for the viewer's PV connection to settle, then returns
the screen as an inline PNG plus `{width, height, pv}`. `pv` is `connected`
only when live PVs were reached, and `disconnected` otherwise, mock mode
included. Nothing is written to disk.

The page is locked to the server's own origin. A request to any other origin
is aborted, and so is a same-origin request that is not `GET`, `HEAD` or
`OPTIONS`. The access credential is added to each same-origin request and
never follows a redirect.
