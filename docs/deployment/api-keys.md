# API keys

Every data endpoint in Canopy is gated by an API key. This page is the
operator's runbook: how to mint, list, and revoke keys, and how the two
credential layers behind the scenes fit together.

## How keys work

Every key carries three independent scopes:

- **`read`** — GET requests and WebSocket connections, plus the handful of
  POSTs that only read (Converter's conversions, Fox's evaluate/validate).
- **`write`** — endpoints that change something: setting a PV, saving a
  snapshot, storing a config.
- **`admin`** — key management (this page's own HTTP surface) and other
  operational-not-data actions, such as dropping a Vole ring buffer.

`admin` is **orthogonal**, not a level above `read`/`write` — an admin key that
cannot itself read a PV is a better credential for a provisioning tool than one
that can do everything. A `write` key cannot mint or revoke keys; only `admin`
can.

Clients send the token in the `X-API-Key` header. WebSocket endpoints also
accept `?api_key=<token>` for browsers that cannot set headers on a WebSocket
handshake — that parameter exists purely as a compatibility workaround and is
not a convention to extend to other endpoints.

### Two credential layers

Keys resolve from two places, checked in order:

1. **Env (`CANOPY_API_KEYS`)** — a small, long-lived, statically-stable set.
   Parsed once at startup, so it keeps working even if Postgres is completely
   unreachable, including on a cold start when there is nothing cached yet.
   Checked first, so the highest-volume credential in the system — the read
   key baked into every display — never makes a database round trip.
2. **Database** — every other key: per-tool, per-user, and CI keys, and any
   `write` key. Minted and revoked without a deploy, cached briefly for
   latency, with revocation therefore not quite instant (see
   [Revocation](#revocation)).

A deployment's env layer typically holds exactly two keys — one `read` key for
displays, one `admin` key for ops — and manages everything else in the
database. The env key is not an emergency-only credential: because the display
key ends up inlined into a public JS bundle at build time (via react-trace's
`.env`), it was never going to be a fast-revocation secret regardless of where
it lived, so it's simplest to keep it as a permanent, statically-stable floor
rather than a special break-glass case.

!!! warning "Unset means locked, not open"
    If both layers are empty, no keys exist and every gated endpoint returns
    `401`. Startup logs the loaded env count — see [Verifying](#verifying-a-deployment).

!!! note "Running without a database"
    An env key works with no Postgres at all. An **unknown** token, however,
    falls through to the database registry, so with no database reachable it
    returns `503` `key_store_unavailable` rather than `401` — the store
    genuinely cannot say whether the token is valid.

## Provisioning a key

### Local development / Docker Compose

`.env` / `docker/.env` carry `CANOPY_API_KEYS`; `docker-compose.yml` defaults it
to `dev:read+write` when unset. Fine for a laptop, never for a shared host.
Everything else — the working set of keys for local testing — is easiest to
manage through the CLI once `CANOPY_DATABASE_URL` points somewhere real:

```bash
# Mint. -r/-w/-a add scopes; at least one is required.
uv run python scripts/create_key.py trace -r
uv run python scripts/create_key.py operator-tools -r -w
uv run python scripts/create_key.py ops-cli -a

# List (add -a for active-only).
uv run python scripts/list_keys.py

# Revoke.
uv run python scripts/deactivate_key.py <key-id>
```

`create_key.py` prints the plaintext token once — it is not recoverable, only
its hash is stored. These scripts talk to the database directly, so they work
with only `CANOPY_DATABASE_URL` set: no existing key is needed to bootstrap the
first one, which is what makes them the right tool for a fresh instance.

Without a local `uv` environment, run the same commands inside the `api`
container instead: `docker compose run --rm api python scripts/create_key.py ...`.

### Facility instances

The env layer's two long-lived keys still live in the per-instance
`APP_SECRETS_<INSTANCE>` GitHub secret's `_shared` block (see
[facility deployment](facilities.md#prerequisite-app_secrets_instance-github-secrets)):

```json
{
  "_shared": {
    "CANOPY_API_KEYS": "<display-read-token>:read,<ops-admin-token>:admin"
  }
}
```

Everything else for that instance — per-tool keys, CI keys — is minted with
the CLI against the instance's database, not through the secret. The CLI ships
in the image (`Dockerfile.prod` copies `scripts/`), so run it in the running
`api` container rather than from a workstation — the facility host's Postgres
is bound to `127.0.0.1` and isn't reachable from outside it:

```bash
docker compose run --rm api python scripts/create_key.py "<app>" -r
```

Rotating an env key is still a deploy; rotating anything else is a CLI call.

## Managing keys over HTTP

The same operations are available at `/v1/api-keys` on the parent app, gated on
`admin` (never `write` — see [Scope design](#why-admin-not-write) below):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/api-keys` | Mint a key. Body: `appName`, and any of `read`/`write`/`admin`. Returns the plaintext `token` once. |
| `GET` | `/v1/api-keys` | List keys (`?activeOnly=true`, `?appName=<name>` to filter). Never returns a token. |
| `GET` | `/v1/api-keys/count` | Count keys (`?activeOnly=true` to filter). |
| `DELETE` | `/v1/api-keys/{id}` | Deactivate a key. |

```bash
curl -X POST -H "X-API-Key: <admin-token>" -H "Content-Type: application/json" \
  -d '{"appName": "trace", "read": true}' \
  http://localhost:<API_PORT>/v1/api-keys
```

!!! note "Deactivating the key you're using"
    `DELETE /v1/api-keys/{id}` refuses to deactivate the key that authenticated
    the request itself — `400` `api_key_self_deactivation` — since doing so
    would lock that session out mid-use. Pass `?force=true` to override, or use
    `scripts/deactivate_key.py`, which has no such guard (it isn't a request
    that can lock itself out).

### Why `admin`, not `write`

Gating key management on `write` would let any write-capable client — every
tool that can set a live setpoint — mint credentials that outlive rotation of
its own key. `write` is the largest, least-controlled population of keys in
Canopy, and the wrong one to trust with provisioning. `admin` is a separate,
narrowly-held scope for exactly this and other operational actions — for the
same reason, `POST /vole/v1/_debug/evict/{pv}` (dropping a ring buffer) is
gated on `admin` too, not `write`: it's an operational action, not a data
write.

## Rotation

Overlapping keys make rotation zero-downtime:

1. **Mint** the new key (`scripts/create_key.py` or `POST /v1/api-keys`). Both
   old and new tokens are now valid.
2. **Roll clients over** to the new token.
3. **Deactivate** the old key (`scripts/deactivate_key.py` or
   `DELETE /v1/api-keys/{id}`).

`last_used_at` (visible via `GET /v1/api-keys`) is what turns "is it safe to
deactivate the old key yet?" from a guess into a query — check it before step 3
rather than picking an arbitrary overlap window.

Rotating an **env** key is still the old process: add the new token to
`CANOPY_API_KEYS`, deploy, roll clients, remove the old token, deploy again.
Keep the env layer's population small (the two long-lived keys) specifically
so this stays rare.

### Revocation

Deactivating a database-backed key (CLI or HTTP) takes effect once its cached
entry expires — the cache TTL **is** the revocation latency (`CANOPY_API_KEY_CACHE_TTL_S`,
default 60s). If Postgres itself is unreachable when you deactivate a key, the
cached entry may still be served stale for up to `CANOPY_API_KEY_MAX_STALE_S`
(default 1h) — bounded so an outage cannot keep a revoked key alive forever,
but worth knowing if a leak coincides with a database outage.

Revoking an **env** key is a deploy, same as rotation: the practical exposure
window is the length of your deploy pipeline.

## Production hygiene

Before declaring an instance provisioned:

- [ ] `CANOPY_API_KEYS` does **not** contain the `dev:read+write` default.
- [ ] Tokens are freshly generated per instance — not shared between DEV and a
      facility, and not shared with another SLAC app.
- [ ] Only one `admin` key exists in the env layer (for ops), and it is not
      also used as a `read`/`write` credential anywhere else.
- [ ] `CANOPY_DEBUG` is `false` (verbose errors otherwise).
- [ ] The env tokens are recorded in a secrets manager; database-backed tokens
      are recorded wherever the owning team tracks its own credentials.

## Verifying a deployment

The startup log reports what the env layer loaded, without ever printing a
token:

```
Registered 2 API key(s) from CANOPY_API_KEYS (1 read, 0 write, 1 admin)
```

An empty env layer is not itself a problem — it just means every key comes
from the database — and logs an `INFO` line saying so rather than a warning. A
malformed entry, an empty token, or an unrecognized scope (`read+wrote`, say)
each warn and name their position, never their content:

```bash
# docker-compose.prod.yml sets no container_name; `compose logs` resolves the
# service name for you rather than requiring you to guess the container's.
docker compose logs api --since 10m | grep CANOPY_API_KEYS
```

The container's healthcheck hits `/health` every 10s, so a plain `--tail 50`
scrolls the startup summary out of view within minutes of boot — use
`--since` (anchored to the deploy time) instead, or reach for Actions ->
`Container Host Maintenance` -> `app-logs` (raise `log_tail`) if you're
working from CI rather than the host.

Then confirm end to end, from a client's point of view:

```bash
# Ungated — proves the process is up, says nothing about keys:
curl http://localhost:<API_PORT>/health

# Read-gated — 200 means the key is valid and has read scope:
curl -H "X-API-Key: <token>" http://localhost:<API_PORT>/hare/v1/health/summary

# Expect 401 {"code": "invalid_api_key", ...}:
curl -H "X-API-Key: not-a-real-token" http://localhost:<API_PORT>/hare/v1/health/summary

# Admin-gated key management — 200 with the admin token, 403 with anything else:
curl -H "X-API-Key: <admin-token>" http://localhost:<API_PORT>/v1/api-keys
```

A key with the wrong scope returns `403` with code `insufficient_read_access`,
`insufficient_write_access`, or `insufficient_admin_access` — distinguishing
"unknown token" (`401`) from "known token, not allowed here" (`403`).

## Known limitations

- Keys have no expiration (`expires_at`) — nothing enforces one today.
- A key is not scoped to a specific module: a key with `read` works against
  Mole, Hare, Vole, Fox, and every other sub-app alike. Per-module scoping
  (e.g. a Squirrel-only key) is a planned, purely additive follow-up.
