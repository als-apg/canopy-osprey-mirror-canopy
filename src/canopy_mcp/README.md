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
endpoint (`/mcp`), the embedded viewer (`/embed/...`) and the draft screens
(`/drafts/...`).

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
| `CANOPY_CONFIRM_WRITES` | `0` | `1` makes every write the viewer sends ask for one confirmation per click first. |
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

Without `CANOPY_ACCESS_TOKEN_FILE` no route but `/mcp` asks who is calling.
Run such a server on a single-user host, bound to loopback (the default). On a host other users can reach, set the access token:
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

Most tools only read. The authoring tools create, change or delete drafts;
with `CANOPY_AUTHORING=0` the server offers only the tools that read.

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
| `new_screen` | authoring | Saves a whole document as a draft. |
| `patch_node` | authoring | Changes one node by JSON Merge Patch. |
| `add_node` | authoring | Appends one node to a container. |
| `remove_node` | authoring | Removes one node and its children. |
| `delete_draft` | authoring | Deletes a draft. |
| `convert_legacy` | authoring | Converts a `.bob`, `.opi`, `.edl` or `.ui` panel and its sub-screens into drafts. |

No tool writes a PV. Every write to a screen is checked against the rev the
caller last saw, so an agent and a person editing in the viewer cannot
overwrite each other. A library screen (`library/<id>`) can be read, checked
and screenshotted. An authoring tool refuses it with `read_only_store`.

## The skills

A skill tells an agent how to use the tools. `skill/SKILL.md`
(`canopy-panel-builder`) covers building and checking screens. It is the
single source of its text and ships in the wheel as package data. Install it
into a project with:

```sh
python -m canopy_mcp.skill install <dir>
```

This writes the same bytes to
`<dir>/.claude/skills/canopy-panel-builder/SKILL.md` and prints the path.
Running it again is safe. The server's MCP `instructions` point at the skill.

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
