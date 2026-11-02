# Appliance mode

In appliance mode Canopy is not a shared service. A host application starts
one small Canopy per user, inside its own container, and puts it behind its own
login and proxy. There is no Postgres, no Redis, no worker and no facility
pipeline: the host launches two processes from an installed wheel, hands them
their settings in the environment, and stops them when the user's session
ends.

This page describes what the host has to launch and the switches it sets.
How the host spawns a Canopy per user, stages the payload, upgrades and rolls
back is the host's own business and is not covered here.

## The two processes

| Name | Command | Role |
|------|---------|------|
| DATA | `uvicorn canopy.main:app` | The Canopy backend, with only the modules the viewer reads (`CANOPY_MODULES`, for example `hare,fox,mole`). It talks to the control system and holds the API-key registry. |
| FRONT | `python -m canopy_mcp --http` | The panel-builder server. One port serves the viewer, the user's screens, the saved views, the relays to DATA and the MCP endpoint (`/mcp`) the user's agent builds screens through. |

DATA is configured as on the [deployment overview](index.md): the
`CANOPY_<MODULE>__<FIELD>` variables and the [API keys](api-keys.md) apply
unchanged. A host binds it to loopback, so that only FRONT reaches it.

FRONT ships in the same wheel as a second package, `canopy_mcp`, installed
with the `mcp` extra. Its settings are flat `CANOPY_*` names, listed below. It
reads no `.env` file: it is always launched with an explicit environment.
FRONT is a Linux appliance: it runs on POSIX systems only, because its draft
store takes `fcntl` file locks.

A browser never talks to DATA. The viewer that FRONT serves reads live values
through FRONT's relays (`WS /hare/v1/ws/pvs`, `WS /fox/v1/ws/formulas`,
`GET /mole/v1/...`), and FRONT adds the read key on the way upstream, so the
key never reaches the browser.

## The payload

Everything the host image needs comes as plain files in one directory, the
**payload**, written by `scripts/build-payload.sh` of this repository:

| Path | Content |
|------|---------|
| `canopy-<version>-*.whl` | The Canopy wheel (both `canopy` and `canopy_mcp`). |
| `pydmconverter-<version>-*.whl` | The converter `convert_legacy` uses. |
| `viewer/` | The built viewer. Its `ir-map.json` is the widget catalog. |
| `trace/` | The built Trace pages. |
| `spec/` | The staged specification, only when the build was given one (see [`spec_unavailable`](#spec_unavailable)). |
| `STAGED.json` | The commits and versions the payload was built from. Written last: a directory without it is not a payload. |

The host installs the two wheels into its image and points
`CANOPY_VIEWER_DIST` at `<payload>/viewer` and `CANOPY_TRACE_DIST` at
`<payload>/trace`.

## Launching FRONT

```bash
uv sync --extra mcp
uv run playwright install --with-deps chromium   # only for the screenshot tool

CANOPY_VIEWER_DIST=/opt/canopy/payload/viewer \
  python -m canopy_mcp --http --host 127.0.0.1 --port 8765
```

Only the streamable-HTTP transport exists; a launch without `--http` is
refused. `--host` defaults to `127.0.0.1` and `--port` to `8765`.

`--fd N` serves on a listening socket the parent process already holds, passed
down as descriptor `N`, instead of binding `--host` and `--port`. A supervisor
that keeps the socket across restarts loses no connection attempt while the
server is down.

`GET /health` answers `{"status": "healthy"}` and is the one route that never
asks for a credential. It says the server is up and reads nothing from the
backend or from the scheme file. The body is the same in every deployment.

`GET /health/summary` reports the live channels and asks for the credential
like every other route:
`{"status": "ok", "degraded": [], "channels": {"connected": 53, "total": 53}, "reachable": null}`.
`degraded` names each cause: `color-scheme` (the scheme file was rejected),
`data-unavailable` (the backend's summary could not be read; `channels` is
null) or `data-degraded` (the backend reports its data plane degraded).
`channels` counts the channels open panels monitor and is null without live
channels. `reachable` relays the backend connector's own statement of what it
can reach, and is null whenever `channels` is or the connector makes none. The
status code is 200 either way.

The cause of `data-unavailable` is in the panel server's log. It writes
`DATA summary unavailable: <reason>` at WARNING when the backend's summary
becomes unreadable or the reason changes (DEBUG while the same reason repeats)
and `DATA summary available` at INFO when it is read again, so each change of
state is one line.

## Switches

An empty value counts as unset, so a manifest that renders a variable empty
gets the default and not an error.

### Backend and credentials

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_BASE_URL` | `http://127.0.0.1:8000` | Where DATA is reachable. |
| `CANOPY_API_KEY_FILE` | unset | File holding the read-scope key FRONT sends to DATA. When set it is the only source: `CANOPY_API_KEY` is ignored. |
| `CANOPY_ACCESS_TOKEN_FILE` | unset | File holding the credential every request except `GET /health` must carry as `X-Canopy-Access`. Unset means no credential is asked for. |
| `CANOPY_SELF_URL` | loopback on the served port | The address this server is reachable at: the screenshot browser loads its pages from it, and without `CANOPY_URL_PREFIX` the screen URLs the tools return start with it. |
| `CANOPY_PROXY_MARK_HEADERS` | unset | Further request headers, whitespace-separated, that mark a request as having come through the host's proxy. `X-Forwarded-Prefix` always does. |
| `CANOPY_RELAY_CONNECT_RETRY_S` | `0` | Seconds the WebSocket relays keep retrying a DATA that is not accepting connections yet. `0` means one attempt. |

A secret file that is named but cannot supply its secret ends the launch with
a non-zero status before any socket is bound. A file variable never falls back
to another source.

Without `CANOPY_ACCESS_TOKEN_FILE`, `/mcp` is for a local agent only: a request
that arrived through a proxy (it carries `X-Forwarded-Prefix` or a header named
in `CANOPY_PROXY_MARK_HEADERS`) is refused there with 403, while the viewer
routes stay reachable. A host whose proxy marks its requests with a header of
its own lists that header there. With the access token, every route answers
only a request that carries the credential, and the host's proxy is what adds
it.

### Screens

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_DRAFTS_DIR` | `drafts` | The user's personal store, the only place the tools write to. Screens are `<dir>/screens/<id>.screen.json`. |
| `CANOPY_LIBRARY_DIR` | unset | A read-only screen library served beside the personal store. Its screens are `library/<id>` to the tools and `/embed/library/<id>` to the viewer. Unset means no library. |
| `CANOPY_PUSH_DIR` | unset | Where [`request_push`](../how_to/request_push.md) writes its bundles, one directory per request. Unset means the tool answers `push_unavailable`. |
| `CANOPY_VIEWS_DIR` | `<CANOPY_DRAFTS_DIR>/views` | Where the saved views are stored. |
| `CANOPY_COLLECTIONS_DIR` | `<CANOPY_DRAFTS_DIR>/collections` | Where the user's own [collections](../how_to/collections.md) and their selection state are stored. |
| `CANOPY_FACILITY_COLLECTIONS_DIR` | unset | A read-only directory of facility collections shipped with the deployment. Unset means no facility collections. |
| `CANOPY_LIBRARY_IGNORED_NAMES` | unset | Names, whitespace-separated and compared case-insensitively, that a library path may not use besides the built-in rules, such as the files the deployment's own build leaves out. The library listing refuses them and so does `request_push`. |
| `CANOPY_AUTHORING` | `1` | `0` takes the authoring tools off the server and leaves the ones that only read. |
| `CANOPY_WORKSPACE` | `0` | `1` lets an agent read and change the operator's workspace (the viewer's tabs and tiles). Only for one server process serving one operator; see the `canopy_mcp` package README. |

Only the `screens/` subtree of the library is served. A
[report](../how_to/library_report.md) or any other file beside it is a 404.
The manifest lists the library screens after the user's own, and every
authoring tool refuses a library screen with `read_only_store`. The library
changes only on the host: a user asks for a screen to be added with
`request_push`, and an administrator reviews that request there.

### Viewer and Trace

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_VIEWER_DIST` | unset | The built viewer (`index.html` and `ir-map.json`). Without it the viewer routes answer 503 and the tools have no widget catalog. |
| `CANOPY_URL_PREFIX` | unset | The path prefix the host serves this server under (`/panel/canopy`). Set, the screen URLs the tools return are this prefix plus the server path, and the host adds whatever lies in front. A value that is not an absolute path of plain segments without a trailing slash stops FRONT at startup. |
| `CANOPY_PUBLIC_URL` | `CANOPY_SELF_URL` | The absolute base of the screen URLs the tools return, when the agent's browser reaches the server under another address. The API's WebSocket relays accept a browser only from the origins of `CANOPY_SELF_URL` and `CANOPY_PUBLIC_URL`, or from the page's own loopback origin; `Host` must be loopback or one of those URLs' hosts, so a reverse proxy must forward one of them. |
| `CANOPY_HOST_STYLESHEETS` | unset | The host's own stylesheets for the pages FRONT serves behind its proxy: whitespace-separated relative `.css` paths, resolved under the proxy prefix. See the `canopy_mcp` package README. |
| `CANOPY_PV_MODE` | `hare` | `mock` makes the served viewer draw PVs from the mock service instead of DATA. |
| `CANOPY_TRACE_DIST` | unset | The built Trace pages. Unset means no Trace tab. |
| `CANOPY_TRACE_DEFAULT_QUERY` | empty | The query the Trace tab opens with when the URL carries none (`pv=...&range=...`). |
| `CANOPY_CORRELATION_DEFAULT_QUERY` | empty | The query the correlation page opens with when the URL carries none (`x=...&y=...`). |
| `CANOPY_WAVEFORM_DEFAULT_QUERY` | empty | The query the waveform page opens with when the URL carries none (`pv=...`). |
| `CANOPY_CHANNELS_FILE` | unset | A JSON array of channel addresses, served on `GET /channels` for the PV autocomplete of the Trace pages. |
| `CANOPY_HOME_SCREEN` | unset | The library screen the viewer opens on, as its id under `CANOPY_LIBRARY_DIR` (`facility/overview`). FRONT hands it to the viewer in `config.json` as `home`; unset, the key is absent. A value with a leading slash, a `.`, `..` or empty segment, or a backslash stops FRONT at startup. |
| `CANOPY_THEME_FILE` | unset | The color scheme, as described under [Color schemes](color-schemes.md). FRONT hands it to the viewer in `config.json`. |

`CANOPY_CHANNELS_FILE` is a catalog the host writes at build time; Canopy does
not produce it. The file is read again on every request and served with an
ETag over its bytes and `Cache-Control: no-cache`. When the variable is unset
or the file is absent the route answers 404: no catalog, not an empty one.

### Checks

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_SPEC_DIR` | `spec/` beside `CANOPY_VIEWER_DIST` | The staged specification the `check` tool validates screens against. |

The directory holds one screen-format schema per version
(`ir/screen-ir-<version>.schema.json`), one property schema per widget
(`widgets/<ir id>.props.schema.json`) and a `SPEC-SOURCE` file naming the
repository and commit they were taken from. With the payload layout above,
`<payload>/spec` is found without setting the variable. What happens when the
directory is missing is described under
[`spec_unavailable`](#spec_unavailable).

### Writes

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_PUT_RELAY` | `0` | `1` relays `PUT /hare/v1/pvs/{pv}` to DATA. It is the one write route FRONT has. |
| `CANOPY_WRITE_API_KEY_FILE` | unset | File holding the write-scope key the relay sends upstream. There is no environment form of this key. |
| `CANOPY_PUT_RELAY_TIMEOUT_S` | `27` | Seconds one relayed write may take upstream before FRONT answers 504. |
| `CANOPY_CONFIRM_WRITES` | `0` | `1` makes the viewer ask for one confirmation per click before each write (`confirmWrites` in `config.json`). A backend whose heartbeat `writes` state is `asked` is asked whatever this says. |

With `CANOPY_PUT_RELAY` off, a `PUT` to a PV answers 405 like every other
mutating request under the backend's paths, so a write from a screen is
refused.

With it on:

- The relay is only ever served behind the access credential. A launch with
  the relay on and without `CANOPY_WRITE_API_KEY_FILE`, or without
  `CANOPY_ACCESS_TOKEN_FILE`, is refused and nothing is served.
- Exactly `PUT /hare/v1/pvs/{pv}` is relayed. Only the content type and the
  write key go upstream, the body is limited to 64 KiB, and DATA's status and
  body come back unchanged.
- A request from another site (by its `Sec-Fetch-Site` header) is refused with
  403.
- An upstream that does not answer in time is a 504 and one that cannot be
  reached a 502, both with the code `pv_write_unknown`: the write may or may
  not have happened.
- The `check` tool reports a control bound to a machine channel as
  `writes_to_machine`, so a reviewer sees what a screen can write.

No MCP tool writes a PV, with the relay on or off. The relay carries a click
in the viewer and nothing else.

!!! warning "The relay is not an authorization"
    FRONT relays what it is sent. Who may write what is decided by the host
    in front of it and by the write key's scope in DATA. A host that turns the
    relay on has to put its own write policy on the path.

### Screenshot browser

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_BROWSER_SANDBOX` | `0` | `1` launches the screenshot Chromium with its own sandbox. |

The `screenshot` tool renders a screen in a headless Chromium. Nothing
launches at startup: the first screenshot starts the browser and it stops
again 300 s after the last one. A server without Playwright or without
Chromium still starts; its `screenshot` tool then answers that screenshots
are unavailable and every other tool works.

The sandbox is off by default because it needs privileges a container usually
lacks. If Chromium cannot start with `CANOPY_BROWSER_SANDBOX=1`, `screenshot`
answers unavailable and names the switch.

Whatever the sandbox setting, every page is locked to the server's own
origin. A request to any other origin is aborted, and so is a same-origin
request that is not `GET`, `HEAD` or `OPTIONS`, so the browser can never send
a write.

## `spec_unavailable` { #spec_unavailable }

`check` looks at a screen in three ways: against the schema of the screen
format, against the property schema of each widget, and for what the screen
can do (what it writes, runs and opens). The two schemas are not part of this
repository or of the wheel. They belong to the Canopy specification
(`slaclab/canopy-spec`) and reach the server as the staged directory named by
`CANOPY_SPEC_DIR`.

**When no specification is staged** (the payload was built without `SPEC`
and `CANOPY_SPEC_DIR` names no directory), the server says so and never hides
it:

- `check` returns exactly one warning, `spec_unavailable`, naming what is
  missing, and runs neither the schema check nor the property check. Every
  other check runs unchanged: refs, formulas, PV lookup, the semantic pass and
  `writes_to_machine`. A widget type the catalog does not know is still an
  error.
- `list_widgets` and `get_widget` answer from the viewer's catalog and carry a
  `note` with the code `spec_unavailable`. `get_schema` answers
  `{"schema": null, "note": {...}}`.
- No tool raises because of it.

The same applies to a single missing file: a document that declares a version
whose schema is not staged gets the one warning for that version.

A screen is therefore never reported as passing a check that did not run. No
copy of the schemas is bundled with Canopy as a stand-in, and there is no
second source the server falls back to.

The state ends without a code change: build a payload with `SPEC=<dir>` set,
or point `CANOPY_SPEC_DIR` at a staged directory, and restart FRONT. Stage the
specification that matches the viewer build; the server names what is missing
from it (`spec_unavailable`, `undocumented_type`).

## Example: a per-user host

A host that gives each user a Canopy of their own, inside that user's
container, launches FRONT with:

- `CANOPY_BASE_URL`, `CANOPY_SELF_URL`, `CANOPY_API_KEY_FILE` and
  `CANOPY_ACCESS_TOKEN_FILE` for the user's own DATA and proxy, and
  `CANOPY_URL_PREFIX` naming the path the proxy serves the panel under;
- `CANOPY_DRAFTS_DIR`, `CANOPY_VIEWS_DIR`, `CANOPY_COLLECTIONS_DIR` and
  `CANOPY_PUSH_DIR` inside the user's own data, `CANOPY_LIBRARY_DIR` only when
  the deployment has a library, and `CANOPY_FACILITY_COLLECTIONS_DIR` only when
  it ships facility collections;
- `CANOPY_VIEWER_DIST` and, when the payload has one, `CANOPY_TRACE_DIST`;
  `CANOPY_SPEC_DIR` is left unset, so `<payload>/spec` is used when it
  exists;
- `CANOPY_CHANNELS_FILE` naming the catalog its build wrote;
- `CANOPY_PUT_RELAY=1` with `CANOPY_WRITE_API_KEY_FILE` when its proxy puts a
  write policy on the path that answers each relayed request and refuses the
  ones it does not allow (see the warning under [Writes](#writes));
- `CANOPY_CONFIRM_WRITES=1` only to confirm every write under a backend that
  says `on`; a backend that says `asked` is confirmed without it;
- `CANOPY_BROWSER_SANDBOX=0`, since the browser runs inside the user's
  container.

## See also

- [The panel-builder skill](../how_to/panel_builder_skill.md): the tools FRONT
  offers and the guide an agent uses them by.
- [Collections](../how_to/collections.md): the facility and personal
  collections FRONT serves.
- [Report what a screen library can do](../how_to/library_report.md): the
  review record kept beside a library.
- [Ask for a screen to be added to the library](../how_to/request_push.md):
  the bundle `request_push` writes into `CANOPY_PUSH_DIR`.
