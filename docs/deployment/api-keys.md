# API keys

Every data endpoint in Canopy is gated by an API key. This page is the
operator's runbook: how to generate keys, how to get them into a deployed
instance, and how to rotate or revoke one.

## How keys work today

Keys are defined entirely by one environment variable:

```bash
CANOPY_API_KEYS="<token>:<scope>[+<scope>][,<token>:<scope>...]"
```

- Scopes are `read` and `write`. `read` covers GET requests and WebSocket
  connections; `write` covers the endpoints that change something —
  setting a PV, saving a snapshot, storing a config.
- Clients send the token in the `X-API-Key` header. WebSocket endpoints also
  accept `?api_key=<token>` for clients that cannot set headers.
- The variable is parsed **once, at process startup**, into an in-memory
  registry. There is no API, no CLI, and no database table for keys.

That last point drives everything below: **adding, rotating, or revoking a key
is a configuration change plus a restart.** For a facility instance that means
editing a GitHub secret and re-running the deploy workflow.

!!! warning "Unset means locked, not open"
    If `CANOPY_API_KEYS` is empty or unset, no keys exist and every gated
    endpoint returns `401`. The failure mode is closed, but it is quiet — see
    [Verifying](#verifying-a-deployment) for how to confirm keys actually loaded.

## Generating a token

Tokens are opaque strings; Canopy never parses them. Use a long random value:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not hand-write tokens, reuse a token across instances, or reuse one across
client classes.

## Choosing scopes

Issue one key per **client class**, with the minimum scope it needs:

| Client class | Scope | Rationale |
|--------------|-------|-----------|
| Read-only displays, dashboards, plots | `read` | Never writes; a leak cannot move the machine |
| Operator tools that set PVs or save snapshots | `read+write` | Needs both |
| CI, smoke tests, monitoring probes | `read` | Should not be able to write |

`write` on a facility instance means putting setpoints on a live accelerator.
Treat a `read+write` token as machine-control credentials, and prefer minting a
second `read` key over widening an existing one.

## Provisioning

### Local development

Put the key in your `.env` (gitignored) or pass it inline:

```bash
CANOPY_API_KEYS="dev:read+write" uv run uvicorn canopy.main:app
```

### Docker Compose

`docker/.env` carries `CANOPY_API_KEYS`; `docker-compose.yml` defaults it to
`dev:read+write` when unset. Fine for a laptop, never for a shared host.

### Facility instances

Keys live in the per-instance `APP_SECRETS_<INSTANCE>` GitHub secret, which is
the only source of the deployed app's environment (see
[facility deployment](facilities.md#prerequisite-app_secrets_instance-github-secrets)).
`CANOPY_API_KEYS` belongs in the `_shared` block, which applies to whichever
facility block that instance uses. Each instance has its own secret, so every
instance gets its own distinct tokens:

```json
{
  "_shared": {
    "CANOPY_API_KEYS": "<read-write-token>:read+write,<read-only-token>:read"
  }
}
```

Then: Actions --> `Deploy` --> pick the instance --> run. Keys take effect when
the container restarts.

Two constraints inherited from the deploy pipeline:

- The value must be **single line** — each key of the secret becomes one
  `KEY=value` line in the app's `.env`.
- The deploy workflow log-masks every string value longer than 4 characters, so
  a properly long token will not appear in the deploy logs.

## Rotation

The mechanism supports overlapping keys, so rotation does not require downtime —
but every step is a deploy:

1. **Add** the new token alongside the old one, comma-separated, in
   `APP_SECRETS_<INSTANCE>`. Deploy. Both tokens are now valid.
2. **Roll clients over** to the new token.
3. **Remove** the old token from the secret. Deploy again. The old token is now
   rejected.

!!! warning "The overlap window is a judgement call, not a measurement"
    Canopy does not record which key served a request — keys have no name and
    nothing logs or counts their use. There is no way to confirm from the server
    that every client has moved off the old token before you remove it. Until
    the persistent registry lands (see [Limitations](#limitations-and-migration-path)),
    keep an inventory of which client class holds which key out-of-band, and
    keep the overlap window long enough to cover every one of them.

### Revocation

Revoking a leaked token is step 3 on its own: remove it from the secret and
deploy. **The token stays valid until that deploy completes**, so the practical
exposure window is the length of your deploy pipeline. Factor that into any
incident response — if the leak is serious, rotate the whole instance's keys
rather than waiting to identify the single affected client.

## Production hygiene

Before declaring an instance provisioned:

- [ ] `CANOPY_API_KEYS` is set, and does **not** contain the `dev:read+write`
      default from `.env.example` / `docker-compose.yml`.
- [ ] Tokens are freshly generated per instance — not shared between DEV and a
      facility, and not shared with another SLAC app.
- [ ] At most one `read+write` key, and only if something actually writes.
- [ ] `CANOPY_DEBUG` is `false` (verbose errors otherwise).
- [ ] The tokens are recorded in a secrets manager, not a wiki page or a chat
      message.

## Verifying a deployment

The startup log reports what loaded, without ever printing a token:

```
Registered 2 API key(s) from CANOPY_API_KEYS (2 read, 1 write)
```

A malformed entry is skipped with a warning naming its position (e.g.
`Skipping malformed CANOPY_API_KEYS entry #2`), and an entry with an
unrecognized scope — `read+wrote`, say — is skipped the same way rather than
registered with whatever scope it did recognize. Check for these after any
change to the secret:

```bash
docker logs canopy-<instance>-api --tail 50 | grep -i "api key"
```

Then confirm end to end, from a client's point of view:

```bash
# Ungated — proves the process is up, says nothing about keys:
curl http://localhost:<API_PORT>/health

# Read-gated — 200 means the key is valid and has read scope:
curl -H "X-API-Key: <token>" http://localhost:<API_PORT>/hare/v1/health/summary

# Expect 401 with {"code": "invalid_api_key", ...}:
curl -H "X-API-Key: not-a-real-token" http://localhost:<API_PORT>/hare/v1/health/summary
```

A key with the wrong scope returns `401` with code `insufficient_read_access`
or `insufficient_write_access`, which distinguishes "unknown token" from
"known token, not allowed here".

## Limitations and migration path

The current registry (`canopy.core.security.EnvKeyRegistry`) is an explicit
stop-gap. Its known limits:

- Keys can only be added, changed, or revoked by editing configuration and
  restarting the process.
- Tokens are stored in plaintext in the deploy secret and in the container's
  `.env`.
- Keys have no name, no creation date, and no usage record, so rotation cannot
  be verified and requests cannot be attributed to a client.

A persistent, Postgres-backed registry is planned to address all three: keys
minted through a CLI or an authenticated endpoint, stored as a hash with the
plaintext shown once, carrying an app name and a last-used timestamp.

It lands behind the same `canopy.core.security.KeyRegistry` protocol that the
current implementation satisfies, so **nothing changes for clients** — the
`X-API-Key` header, the two scopes, and the error codes above all stay as they
are. What changes is the operator workflow: provisioning and revocation stop
requiring a deploy.
