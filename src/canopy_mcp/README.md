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
endpoint (`/mcp`), the embedded viewer (`/embed/...`) and the draft screens
(`/drafts/...`).

## Screenshots

`screenshot(id, macros)` loads this server's own `/embed/drafts/<id>?<macros>`
in the pv mode the server is configured for (`CANOPY_PV_MODE`). It waits up to
4 s for web fonts and for the viewer's PV connection to settle, then returns
the screen as an inline PNG plus `{width, height, pv}`. `pv` is `connected`
only when live PVs were reached, and `disconnected` otherwise, mock mode
included. Nothing is written to disk.
