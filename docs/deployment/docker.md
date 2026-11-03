# Docker Compose

Two compose files live under `docker/`:

- `docker-compose.yml` — the **development stack**: API + worker built from
  source with hot reload, plus bundled Postgres and Redis.
- `docker-compose.prod.yml` — the **production app**: API + worker from a
  prebuilt image, no data layer. The reference for
  [deploying at your facility](production.md).

## Development stack

```bash
cd docker
cp ../.env.example .env     # optional — sensible defaults are baked in
docker compose up -d
```

This brings up four services:

| Service | Image | Notes |
|---------|-------|-------|
| `api` | built from `Dockerfile.dev` | Uvicorn with `--reload`; `src/` and `tests/` are bind-mounted, so edits apply live |
| `worker` | built from `Dockerfile.dev` | Arq worker for background jobs |
| `postgres` | `postgres:16-alpine` | Data persists in the `canopy-pgdata` volume |
| `redis` | `redis:7-alpine` | Job queue |

Defaults (overridable in `docker/.env`): API on port `8000`
(`API_HOST_PORT`), key `dev:read+write`, `simulation` EPICS backend, debug
on. Verify:

```bash
curl http://localhost:8000/health
curl -H "X-API-Key: dev" http://localhost:8000/hare/v1/pvs/DEMO:TEMP
```

### Database migrations

Run Alembic from the **host**, pointed at the published Postgres port — the
dev image copies only `src/`, so the migration tree is not inside the
container:

```bash
CANOPY_DATABASE_URL=postgresql+asyncpg://canopy:canopy@localhost:5432/canopy \
  uv run alembic upgrade head
```

Postgres-backed endpoints (Squirrel) return errors until this has been run
once.

### Defining facilities

Facilities are not seeded by a migration and are not read from an env var — a
facility is a row in `core.facility`, created explicitly. Nothing can reference
a facility slug that doesn't exist: minting an API key with an unknown grant is
a 400. (A PV or snapshot write naming an unknown slug is also rejected, once
Squirrel's facility enforcement — not yet merged — reads this table.)

For the dev stack, seed the SLAC hierarchy in one step (idempotent, safe to
re-run):

```bash
CANOPY_DATABASE_URL=postgresql+asyncpg://canopy:canopy@localhost:5432/canopy \
  uv run python scripts/seed_dev_facilities.py
```

This is **dev only** — it is deliberately not wired into container startup.
Each production instance has its own database and its own hierarchy, so there
is no single baseline to seed, and re-seeding on boot would fight facilities
edited through the API. Define them individually instead:

```bash
uv run python scripts/create_facility.py lcls --name "LCLS"
uv run python scripts/create_facility.py lcls-sc --name "LCLS-II SC" --parent lcls
uv run python scripts/list_facilities.py
```

Slugs are lowercase alphanumerics with single internal hyphens — not leading,
trailing, or doubled; `LCLS` is rejected rather than silently lowercased. A
handful of slugs (`snapshots`, `tags`, `pv-browser`, `api-keys`, `meta-pvs`,
`comparison`, `snapshot-details`) are reserved, since they collide with
react-squirrel's own page routes. A slug is **immutable** once created — it's
what `?facility=` filters and API key grants reference — so correcting one
means deleting and recreating, which is refused while the facility has
children or while any PV, snapshot, or active key grant still references it.

An `admin` key can also manage facilities over HTTP: `POST /v1/facilities`,
`PUT /v1/facilities/{slug}`, `DELETE /v1/facilities/{slug}`. These routes
additionally require the key to hold the facility wildcard (`*`), not just
`admin` — reparenting or deleting a facility can widen what an existing grant
on an ancestor resolves to, so only an unrestricted key may touch the
hierarchy at all. The CLI stays the bootstrap path, since it needs no key.

### Using a local IOC

To point the containers at an IOC on the host, set the EPICS variables in
`docker/.env`:

```bash
CANOPY_EPICS__BACKEND=epics
EPICS_CA_ADDR_LIST=host.docker.internal
EPICS_CA_AUTO_ADDR_LIST=NO
```

### Housekeeping

```bash
docker compose logs -f api      # follow API logs
docker compose down             # stop (keeps the Postgres volume)
docker compose down -v          # stop and wipe data
```

## Production compose

`docker-compose.prod.yml` runs only the app: an `api` service and two
`worker` replicas, both from the image named by `IMAGE`
(e.g. `ghcr.io/slaclab/canopy:<tag>`).

It differs from the dev stack deliberately:

- **No Postgres/Redis.** Run Postgres 16 and Redis 7 however your site runs
  databases, and point `CANOPY_DATABASE_URL` / `CANOPY_REDIS_URL` at them —
  see [Postgres and Redis](production.md#postgres-and-redis).
- **`network_mode: host`.** EPICS Channel Access / PV Access need UDP
  broadcast, which bridged container networking breaks. The API listens on
  `API_PORT` (default 8000) directly on the host.
- **All config from a sibling `.env` file.** Compose reads it for `IMAGE`
  and `API_PORT` and passes it to both services as their environment; nothing
  is baked into the image or the compose file. Write it by hand or render it
  from your secret store.

The production image is built from `docker/Dockerfile.prod` by CI on every
push to `main` and every `v*` release tag — see
[image tags](production.md#image-tags). The image also carries the Alembic
tree, so migrations run as
`docker compose run --rm api alembic upgrade head`.

On the host, save the file as `compose.yml` next to its `.env`, so the bare
`docker compose` commands in these docs find it. Compose doesn't pick up
`docker-compose.prod.yml` by itself, and in the repo's `docker/` directory the
same commands select the development stack. See
[Deploy at your facility](production.md#run-with-the-reference-compose-file).
