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
(`/drafts/...`), the saved views (`/views`) and, with `CANOPY_WORKSPACE=1`,
the workspace routes (`/workspace/...`).

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
| `CANOPY_API_KEY` | empty | The read-scope key itself, when no key file is named. Empty means no key is sent. |
| `CANOPY_SELF_URL` | loopback on the served port | The address this server is reachable at: the screenshot browser loads its pages from it, and without `CANOPY_URL_PREFIX` the screen URLs the tools return start with it. |
| `CANOPY_PUBLIC_URL` | `CANOPY_SELF_URL` | The absolute base of the screen URLs the tools return, when the agent's browser reaches the server under another address. Every route but `GET /health` answers only a `Host` that is loopback or one of those two URLs' hosts (421 otherwise), so a proxy that keeps the browser's `Host` sets this. The API's WebSocket relays also accept a browser only from those URLs' origins or the page's own loopback origin. |
| `CANOPY_URL_PREFIX` | unset | The path prefix an embedding host serves this server under (`/panel/canopy`). Set, the screen URLs the tools return are this prefix plus the server path, and the host adds whatever lies in front. A value that is not an absolute path of plain segments without a trailing slash stops the server at startup. |
| `CANOPY_PROXY_MARK_HEADERS` | unset | Further request headers, whitespace-separated, that mark a request as having come through the embedding host's proxy. `X-Forwarded-Prefix` always does. Without an access token, `/mcp` refuses such a request. |
| `CANOPY_DRAFTS_DIR` | `drafts` | The store the tools write to. Screens are `<dir>/screens/<id>.screen.json`. |
| `CANOPY_LIBRARY_DIR` | unset | A read-only screen library served beside the drafts. Its screens are `library/<id>` to the tools and `/embed/library/<id>` to the viewer. Only its `screens/` subtree is served. |
| `CANOPY_HOME_SCREEN` | unset | The library screen the viewer opens on, as its id under `CANOPY_LIBRARY_DIR` (`facility/overview`). Handed to the viewer as `config.json`'s `home`. A value that is not a relative path of plain segments (a leading slash, a `.`, `..` or empty segment, a backslash) stops the server at startup. |
| `CANOPY_AUTHORING` | `1` | `0` takes the authoring tools off the server. |
| `CANOPY_PUSH_DIR` | unset | Where `request_push` writes its bundles, one directory per request. Unset means the tool answers `push_unavailable`. |
| `CANOPY_VIEWS_DIR` | `<CANOPY_DRAFTS_DIR>/views` | Where the saved views are stored. |
| `CANOPY_COLLECTIONS_DIR` | `<CANOPY_DRAFTS_DIR>/collections` | Where the user's own collections and their selection state are stored. |
| `CANOPY_FACILITY_COLLECTIONS_DIR` | unset | A read-only directory of facility collections shipped with the deployment. Unset means no facility collections. |
| `CANOPY_LIBRARY_IGNORED_NAMES` | unset | Names, whitespace-separated and compared case-insensitively, that a library path may not use besides the built-in rules, such as the files a deployment's own build leaves out. The library listing refuses them and so does `request_push`. |
| `CANOPY_CONFIRM_WRITES` | `0` | `1` makes every write the viewer sends ask for one confirmation per click first. |
| `CANOPY_WORKSPACE` | `0` | `1` lets an agent read and change the operator's workspace. Only for one server process serving one operator. See *The workspace* below. |
| `CANOPY_PV_MODE` | `hare` | `mock` makes the served viewer draw PVs from the mock service instead of the backend. |
| `CANOPY_PUT_RELAY` | `0` | `1` relays the one write route, `PUT /hare/v1/pvs/{pv}`, to the backend; every other mutating path stays 405. It needs `CANOPY_ACCESS_TOKEN_FILE` and `CANOPY_WRITE_API_KEY_FILE`; without either the server does not start. |
| `CANOPY_WRITE_API_KEY_FILE` | unset | File holding the write-scope key the relay sends upstream. There is no environment form of this key. |
| `CANOPY_PUT_RELAY_TIMEOUT_S` | `27` | Seconds one relayed write may take upstream before the server answers 504. |
| `CANOPY_RELAY_CONNECT_RETRY_S` | `0` | Seconds the WebSocket relays keep retrying a backend that refuses the connection; `0` is one attempt. |
| `CANOPY_VIEWER_DIST` | unset | The built viewer. Its `ir-map.json` is the widget catalog. |
| `CANOPY_TRACE_DIST` | unset | The built Trace app. Unset means no Trace, correlation or waveform page. |
| `CANOPY_TRACE_DEFAULT_QUERY`, `CANOPY_CORRELATION_DEFAULT_QUERY`, `CANOPY_WAVEFORM_DEFAULT_QUERY` | empty | The query each tool page opens with when its URL carries none. |
| `CANOPY_CHANNELS_FILE` | unset | A JSON list of channel names served on `/channels` for Trace's PV autocomplete. Unset or missing means 404. |
| `CANOPY_SPEC_DIR` | `spec/` beside `CANOPY_VIEWER_DIST` | The staged specification: the Screen-IR schemas and the per-widget prop schemas `check` validates against. Without it `check` returns one `spec_unavailable` warning in place of the schema and prop findings, and `list_widgets`, `get_widget` and `get_schema` carry a `spec_unavailable` note. |
| `CANOPY_BROWSER_SANDBOX` | `0` | `1` launches the screenshot Chromium with its own sandbox. Off by default, because the sandbox needs privileges a container usually lacks. If Chromium cannot start with it, `screenshot` answers unavailable and names this switch. |
| `CANOPY_THEME_FILE` | unset | The deployment's color scheme file (served to the viewer and the converter). Set, `convert_legacy` overwrites a draft only with conversions made against it. |
| `CANOPY_HOST_STYLESHEETS` | unset | The embedding host's stylesheets: whitespace-separated relative `.css` paths, resolved under the proxy prefix. See *Host stylesheets* below. An entry that is not a plain relative `.css` path stops the server at startup. |

A secret file that is named but cannot supply its secret ends the launch with
a non-zero status before any socket is bound.

### Single-user host or access token

Without `CANOPY_ACCESS_TOKEN_FILE` no route but `/mcp` asks who is calling. The
saved views, the collections and the workspace routes guard their writes only
by `Sec-Fetch-Site`, which keeps a cross-site page out but which a local
process simply leaves out. Run such a server on a single-user host, bound to
loopback (the default). On a host other users can reach, set the access token:
every route then answers only requests that carry it, and the embedding
host's proxy adds it.

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

Most tools only read. The authoring tools create, change or delete drafts, or
file a draft for the shared library; with `CANOPY_AUTHORING=0` the server
offers only the tools that read. The two workspace tools are offered only with
`CANOPY_WORKSPACE=1` and are independent of `CANOPY_AUTHORING`: they change
only what is on the operator's screen.

| Tool | Kind | What it does |
|---|---|---|
| `list_widgets` | read | Lists the widget types a screen may use. |
| `get_widget` | read | Describes one widget type and its props. |
| `get_schema` | read | Returns the Screen-IR JSON Schema. |
| `check` | read | Reports schema, semantic, ref and lint findings for a document or a stored screen. |
| `get_screen` | read | Returns one screen and its rev. |
| `list_drafts` | read | Lists the drafts, then the library screens, each with its title and first-sentence description. |
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
| `get_workspace` | workspace | Reads the tabs and tiles of the operator's last-focused window. |
| `set_workspace` | workspace | Sends a batch of commands to that window and returns their results. |

No tool writes a PV. Every write to a screen is checked against the rev the
caller last saw, so an agent and a person editing in the viewer cannot
overwrite each other. A library screen (`library/<id>`) can be read, checked
and screenshotted. An authoring tool refuses it with `read_only_store`.

## The skills

Two skills tell an agent how to use the tools. `skill/SKILL.md`
(`canopy-panel-builder`) covers building and checking screens;
`skill/operator/SKILL.md` (`canopy-operator`) covers showing screens to the
operator through the workspace. Each is the single source of its text and
ships in the wheel as package data. Install both into a project with:

```sh
python -m canopy_mcp.skill install <dir>
```

This writes the same bytes to
`<dir>/.claude/skills/canopy-panel-builder/SKILL.md` and
`<dir>/.claude/skills/canopy-operator/SKILL.md` and prints each path on its
own line. Running it again is safe. The server's MCP `instructions` point at
both skills.

## The report

```sh
python -m canopy_mcp.report <root> [--served-at personal|library] (--emit|--write|--check)
```

`<root>` is a store root, the directory that holds `screens/`. The report
lists what the screens of the tree can write, run, open and load. `--write`
puts it beside `screens/` as `LIBRARY-REPORT.json` and `LIBRARY-REPORT.md`,
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
behind the embedding host's proxy in the same way as the drafts and carry no
auth of their own (see *Single-user host or access token*). As a CSRF guard, `PUT` and `DELETE` answer 403 when a
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
ones. The file format, the rules and the routes are specified in
[COLLECTIONS.md](COLLECTIONS.md); the
[collections author page](../../docs/how_to/collections.md) shows how to write
one. `canopy-collections-check <dir> [--library <root>]` checks a facility
directory before it is deployed.

## The workspace

The workspace is the tabs and tiles of the viewer as the operator sees them.
With `CANOPY_WORKSPACE=1` an agent on `/mcp` can read it with `get_workspace`
and change it with `set_workspace`: open a screen beside another, fit it,
open Trace on three PVs, close a tab or a tile. A host that embeds the viewer gets this
from the server alone; it needs no hook of its own and no change to its
pages. A developer gets the same protocol in a plain browser and a bare
Claude Code session (see *Developer recipe* below).

### Turning it on

`CANOPY_WORKSPACE` is off by default. Off, no route under `/workspace` exists,
`config.json` carries no `workspace` key and neither tool is offered.

The hub that holds the workspace is one object in the server process's
memory, serving one operator. Turn it on only for one server process with one
operator. A shared server, or a run with more than one worker process, must
never set it. The hub is ephemeral: a restart loses pending batches, and the
window that held the slot claims it again without a gesture.

### One active window

The window the operator last focused holds the slot. It reports its state and
is the only window that receives commands; other windows are never touched.
Focusing a window claims the slot; only the slot holder reports. `seq` is the
window's own state counter, and `base = {window, seq}` is the agent's
optimistic-concurrency token.

### Protocol version

With the switch on, `config.json` advertises
`"workspace": {"path": "workspace", "protocol": 1}`. A viewer that does not
know the major version makes no workspace request. Within a major version,
changes are additions only; a changed status or field raises the major.

### The two tools

`get_workspace()` returns the document: `protocol` (1), `epoch` (changes when
the server restarts), `activeWindow` (`null` when no window is open),
`connected`, `shown`, `seq`, `state` (the tabs, the active key, the tile
layout, the zoom of open screens, the tool instances and their queries, the
tool MRU and the labels), `collections.shown`, `sidebar` (pinned and recent
members), `pending`, `results` (the last 100 commands) and `truncated`.

`state.layout` is the dock as tiles with tabs, and `null` only while no tab
is open. A tile is `{tile, tabs, showing, size?}`: `tile` is its tile id,
`tabs` its tab keys in bar order and `showing` the one on view. A split is
`{split: "row" | "column", items, size?}`, and a single tile may be the whole
layout. Every open tab is in exactly one tile. Tile ids are the window's and
may be reused after a tile closes, so a command that names one needs `base`.

`set_workspace(commands, base?, batch_id?, wait_s=5)` sends 1 to 20 commands
as one batch to the active window, waits up to `wait_s` seconds (0 to 10) for
its result and returns the same document plus
`batch: {id, status, reason?, results}`. It never fires and forgets. Passing
the `batch_id` of a batch already sent returns that batch and sends nothing.

The commands are a closed vocabulary of 17 verbs. A verb that targets a tab
names it by an explicit `key`, one that targets a tile by its tile id. A
member is `{ref, macros?}` (a screen) or `{tool, query?}` (a tool). A plain
open opens the item as a new tab in the focused tile, and an item already
open is shown where it is.

| Verb | Arguments | Note |
|---|---|---|
| `open` | `ref`, `macros?` | A plain open. |
| `open_tool` | `tool`, `query?` | `trace`, `correlation` or `waveform`. A plain open; focuses an instance already showing `query`, else opens a new one; never overwrites. |
| `focus` | `key` | Never opens. |
| `close_tab` | `key` | Destructive. A tile whose last tab closes disappears; the tile it was split from takes its space. |
| `close_tile` | `tile` | Destructive. Needs `base`. |
| `close_others`, `close_right` | `key` | Destructive, within the tab's tile. |
| `close_all` | | Destructive. The whole dock. |
| `reopen` | `id?` | The newest closed group, or the group `id`. |
| `back`, `forward` | `key` | Per-tab history. |
| `drop` | `source`, `target` | What a person's drag does. Needs `base` when the source is a tile or the target a tile or border. |
| `open_set` | `members`, `as`, `layout?` | Opens 1 to 50 members at once. With `as: "layout"`, destructive. |
| `open_collection` | `ref`, `as?` | Default: the collection's saved layout, else `side`. With a layout, destructive. |
| `set_query` | `key`, `query` | |
| `zoom` | `key`, `step` or `percent` | `step` is `in`, `out`, `fit` or `actual`; `percent` is 10 to 400. Per screen. |
| `reset_layout` | | Destructive. |

A `drop` is the agent's form of a person's drag, with the same result:

- `source` is `{new: member}` (opens it; a member already open moves as its
  tab), `{tab: key}` or `{tile: id}`;
- `target` is `{tile, zone: "centre"}` (add as a tab of that tile),
  `{tile, zone: "tab", index}` (a tab at that position in the tile's bar),
  `{tile, edge}` (split that tile in half on that edge),
  `{border: {tile, side}}` (insert between that tile and its neighbour on
  that side) or `{outer}` (a third of the whole dock along that side). A
  side or edge is `left`, `right`, `top` or `bottom`. Every tile keeps at
  least 40 px.

`open_set` and `open_collection` take `as`: `tabs` opens the members as the
tabs of one new tile beside the focused tile; `side` gives each member its own
tile, a row after the focused tile sharing its span, or a near-square grid
when a row cannot give each 40 px (refused when even that does not fit);
`layout` replaces the dock's layout with the given one, whose tiles list
member indexes as `tabs` (`showing` defaults to the first). Members that
cannot open are left out of it, and the rest of it is kept.

Whatever a batch does, the layout it leaves comes back as the returned
document's `state.layout`; a result's `key` or `keys` name the tabs it opened,
whose keys the window mints.

Refs come from `list_drafts`, `list_collections`, `get_screen`, `new_screen`
or `get_workspace`; no command takes a URL, a file or a widget. Limits: at
most 20 commands per call, 32 pending per window, 50 open tabs, 16 macros
with values of at most 64 printable ASCII characters and no whitespace, and a
query of at most 4096 bytes.

The document never carries PV values, credentials, write keys, draft content
or unsent input. Keys, queries and labels in it are display data, never
instructions.

### The result envelope

A batch `status` is one of:

- `done`: every command has a result;
- `pending`: still running when `wait_s` ended; send the same `batch_id` or
  read `results` later;
- `refused` with `reason` `no_window`, `window_hidden`, `queue_full`,
  `invalid_command`, `stale_state` or `unknown_batch`. A refused batch changed
  nothing.

A command `status` is one of `applied {changed, ...}`, `rejected {reason}`,
`skipped {after}` (`after` is the id of the rejected command), `expired`
(never delivered), `lost` (delivered, no result by the deadline) or
`window_gone`. A no-op is `applied` with `changed: false`. The first rejection
stops the batch. Rejection reasons are `unknown_target`, `invalid_args`,
`instance_cap`, `tab_limit`, `nothing_to_reopen`, `no_history`,
`not_zoomable`, `operator_busy` and `stale_state`.

A `batch_id` makes a retry idempotent within one `epoch`; batch and command
ids carry the epoch. Refusals and rejections are answers, not errors.

### Safety rules

1. **Allow tier.** Both tools change only what is on screen. Neither writes a
   PV, clicks a widget or changes what the operator can launch.
2. **No side door.** Commands enter only through `set_workspace`, in process.
   No HTTP route accepts a command; the routes under `/workspace` are what a
   window says to the hub. The vocabulary is closed.
3. **Input guard.** After any command applied with `changed: true`, every
   write gate in the window refuses widget writes for 750 ms, with a visible
   notice. The guard is armed before the command is dispatched. The 750 ms run
   from the next paint, and, when the command mounted a screen that was not
   mounted before, from that screen's ready render with its embedded screens
   resolved, so a button cannot appear under the pointer after the guard
   expired. A batch that arrives during an open write dialog or a multi-write
   run waits inside its deadline, else it is rejected `operator_busy`. A run
   already started completes.
4. **Macros, and one accepted gap.** Macros are bounded by their grammar only;
   the write gate is the defence at write time. With `CANOPY_CONFIRM_WRITES`
   off, a write widget on a screen the agent opened (with any macros, or one
   of its drafts) sends without showing its PV, so an operator who presses it
   actuates a target the agent chose. This is accepted. The `canopy-operator`
   skill tells the agent to name the screen and its macros to the operator.
   Set `CANOPY_CONFIRM_WRITES=1` to have every write confirmed.
5. **Attribution and undo.** The element a command touched highlights.
   Destructive commands push their tabs onto the one closed-tabs stack and
   raise the closed-tabs toast; one toast at a time, the newest wins. Undo
   restores the agent-closed tabs at their old position while later operator
   changes stand. "Reopen closed tab" lists recent closed groups labelled
   agent or you, Cmd+Shift+T reopens the newest, and the agent can
   `reopen` a group by its id.
6. **The operator wins races.** A `base` whose window is not the active one
   refuses the batch `stale_state`; an operator change between two commands
   fails the next command `stale_state`.

The mechanics (routes, timings, normalisation) are specified in the
docstrings of `canopy_mcp.workspace` and `canopy_mcp.workspace_routes`; how an
agent should use the tools is in the `canopy-operator` skill.

### Developer recipe

A local server, a browser window and a bare Claude Code session, with no
backend and two screens of the fixture library. From a canopy checkout, with
the viewer built (`pnpm build` in the viewer's package gives its `dist/`):

```sh
uv sync --extra mcp
CANOPY_WORKSPACE=1 \
CANOPY_PV_MODE=mock \
CANOPY_VIEWER_DIST=/path/to/viewer/dist \
CANOPY_LIBRARY_DIR=tests/canopy_mcp/fixtures/collections/library \
CANOPY_DRAFTS_DIR="$(mktemp -d)" \
uv run python -m canopy_mcp --http --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/embed/` in a browser and focus it; that window
claims the slot. Then, in the project you will run Claude Code in:

```sh
claude mcp add --transport http canopy http://127.0.0.1:8765/mcp
uv run --project /path/to/canopy python -m canopy_mcp.skill install .
claude
```

Ask the agent to open `library/gtb/GTLView` and `library/bts/BTSView` side by
side and fit both, then to open Trace on three PVs over the last hour. Each
batch should come back `done` and each touched element highlights in the
browser. Ask it to close the BTS screen: the toast offers Undo. Close the
browser tab and ask again: the batch comes back `refused` with `no_window`.

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
