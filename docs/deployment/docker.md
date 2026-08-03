# Docker Compose

Two compose files live under `docker/`:

- `docker-compose.yml` — the **development stack**: API + worker built from
  source with hot reload, plus bundled Postgres and Redis.
- `docker-compose.prod.yml` — the **production app**: API + worker from a
  prebuilt image, no data layer. Used by the
  [facility deployment pipeline](facilities.md).

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

- **No Postgres/Redis.** The data layer is provisioned separately (once per
  instance) and reached over `127.0.0.1:<port>` on the host — see
  [facility deployment](facilities.md#data-layer-init-one-time-per-instance).
- **`network_mode: host`.** EPICS Channel Access / PV Access need UDP
  broadcast, which bridged container networking breaks. The API listens on
  `API_PORT` (default 8000) directly on the host.
- **All config from a sibling `.env` file.** In the facility pipeline that
  file is rendered from the instance's `APP_SECRETS` secret; nothing is baked
  into the image or the compose file.

The production image is built from `docker/Dockerfile.prod` by CI on every
push to `main` — see [image build](facilities.md#deploy) for tags. The image
also carries the Alembic tree, so migrations run as
`docker compose run --rm api alembic upgrade head`.
