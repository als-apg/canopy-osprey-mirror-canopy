# canopy_mcp

The panel-builder MCP server. It ships in the canopy wheel as a second
top-level package, installed with the `mcp` extra, and talks to the canopy
backend over HTTP only.

## Install

```sh
uv sync --extra mcp
uv run playwright install --with-deps chromium
```

The second command is required. The `screenshot` tool renders drafts in a
headless Chromium, which the server launches at startup and keeps for its
lifetime, so a server without Chromium fails to start. It does not wait for the
first screenshot to fail. `--with-deps` also installs the system libraries
Chromium needs on Linux (the canopy image). On a developer Mac,
`uv run playwright install chromium` is enough.

## Run

```sh
CANOPY_VIEWER_DIST=/path/to/viewer/dist python -m canopy_mcp --http --host 127.0.0.1 --port 8765
```

Only the streamable-HTTP transport is supported. The same port serves the MCP
endpoint (`/mcp`), the embedded viewer (`/embed/...`), the draft screens
(`/drafts/...`) and the saved views (`/views`).

## Saved views

A saved view is a named Trace query. The viewer finds the API through
`config.json` (`"views": {"path": "views"}`). Each view is stored as
`<CANOPY_DRAFTS_DIR>/views/<id>.json`, next to `screens/` rather than inside
it, so views never show up in the drafts manifest or under `/drafts`.

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
Writes are atomic (temp file plus `os.replace`).

## Screenshots

`screenshot(id, macros)` loads this server's own `/embed/drafts/<id>?<macros>`
in the pv mode the server is configured for (`CANOPY_PV_MODE`). It waits up to
4 s for web fonts and for the viewer's PV connection to settle, then returns
the screen as an inline PNG plus `{width, height, pv}`. `pv` is `connected`
only when live PVs were reached, and `disconnected` otherwise, mock mode
included. Nothing is written to disk.
