# Deploy at your facility

A production Canopy instance is the published image running as an `api` and a
`worker` process, plus a Postgres 16 database and a Redis 7 server that you
run alongside it. This page is the checklist for standing one up with the
reference compose file. Use the same steps with any other tooling.

## Image tags

CI publishes `ghcr.io/slaclab/canopy` from `docker/Dockerfile.prod`:

| Tag | Built from | Use |
|-----|------------|-----|
| `X.Y.Z` (e.g. `1.2.0`) | A `vX.Y.Z` release tag | Production. Pin this |
| `X.Y` (e.g. `1.2`) | The newest `vX.Y.*` release | Picks up patch releases on the next pull |
| `<short-sha>` (e.g. `abc1234`) | Every push to `main`: the commit's first 7 SHA characters | Test and DEV instances |
| `latest` | The newest `main` build | Moves on every merge. Don't pin it |

Every image carries the commit it was built from in its
`org.opencontainers.image.revision` label, so you can turn `latest` into a
fixed `<short-sha>` tag. Pre-release tags (e.g. `v1.3.0-rc.1`) get only the
full `X.Y.Z-rc.N` tag. Release images start with the first `v*` tag pushed
after the release tagging was added. Older tags have no image.

## Postgres and Redis

Canopy is tested against Postgres 16 and Redis 7, the versions in the
development compose file and CI. They are not in the production compose file:
run them however your site runs databases. Canopy needs:

- A Postgres database owned by the user in `CANOPY_DATABASE_URL`. Alembic
  creates every table, one schema per module, and the app never creates
  tables itself.
- A Redis server for the job queue.

## Configuration

All configuration is environment variables. With the reference compose file
they live in a `.env` file next to it, which compose reads both for its own
`${IMAGE}` / `${API_PORT}` and as the containers' environment.

| Variable | Required | Purpose |
|----------|----------|---------|
| `IMAGE` | Compose | `ghcr.io/slaclab/canopy:<tag>` |
| `API_PORT` | Compose | Port the API listens on, on the host. Default `8000` |
| `CANOPY_DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@host:port/db` |
| `CANOPY_REDIS_URL` | Yes | `redis://[:password@]host:port/0` |
| `CANOPY_API_KEYS` | Yes | The long-lived env keys. See [API keys](api-keys.md) |
| `CANOPY_EPICS__BACKEND` | Yes | `epics`. The default, `simulation`, serves synthetic data |
| `EPICS_CA_ADDR_LIST`, `EPICS_CA_AUTO_ADDR_LIST` | For CA | Your facility's Channel Access network |
| `EPICS_PVA_ADDR_LIST`, `EPICS_PVA_AUTO_ADDR_LIST` | For PVA | Your facility's PV Access network |
| `EPICS_CA_SERVER_PORT`, `EPICS_PVA_SERVER_PORT`, ... | If non-default | Any other standard EPICS variable your network needs |
| `CANOPY_EPICS__CA_TIMEOUT_S`, `CANOPY_EPICS__PVA_TIMEOUT_S` | No | EPICS backend tuning. Default 5 s each |
| `CANOPY_DEBUG` | No | Keep `false` (the default) in production |

The [configuration table](index.md#configuration) and
[`.env.example`](https://github.com/slaclab/Canopy/blob/main/.env.example)
cover every other setting. Each value must be a single line.

```bash
# .env
IMAGE=ghcr.io/slaclab/canopy:1.2.0
API_PORT=8000
CANOPY_DATABASE_URL=postgresql+asyncpg://canopy:<pg-password>@<pg-host>:5432/canopy
CANOPY_REDIS_URL=redis://:<redis-password>@<redis-host>:6379/0
CANOPY_API_KEYS=<display-read-token>:read@*,<ops-admin-token>:admin@*
CANOPY_EPICS__BACKEND=epics
EPICS_CA_ADDR_LIST=<CA address list>
EPICS_CA_AUTO_ADDR_LIST=NO
EPICS_PVA_ADDR_LIST=<PVA address list>
EPICS_PVA_AUTO_ADDR_LIST=NO
```

## Run with the reference compose file

Copy `docker/docker-compose.prod.yml`, from the same release as the image, to
a directory on the host as `compose.yml`, next to the `.env` above. Compose
finds `compose.yml` without `-f`, so the commands on this page work from that
directory. Under its repo name, every command would need
`-f docker-compose.prod.yml`. It runs one `api` and two `worker` replicas with
`network_mode: host`, because EPICS CA/PVA discovery needs UDP broadcast. See
[Production compose](docker.md#production-compose).

```bash
docker compose pull
docker compose run --rm api alembic upgrade head
docker compose up -d
```

Without compose, run the image twice with the same environment: once as
`uvicorn canopy.main:app --host 0.0.0.0 --port <port>` and once as
`arq canopy.worker.WorkerSettings`.

## Upgrade

Migrate before the new image starts serving:

1. Set the new tag in `IMAGE`.
2. `docker compose pull`
3. `docker compose run --rm api alembic upgrade head`
4. `docker compose up -d`

The image carries the Alembic tree, so step 3 runs the new release's
migrations. To roll back, deploy the older tag and skip step 3. Alembic in an
older image fails if the database is already on a revision it doesn't know.

## Verify

```bash
curl http://localhost:<API_PORT>/health
docker compose ps        # api healthy, both workers up
```

`/health` is process liveness, the same probe the compose healthcheck uses.
Each module also has `GET /<module>/v1/health/watchdog`; see
[Health checks](index.md#health-checks). Confirm the WebSocket `heartbeat`
frame reports the `epics` backend, not `simulation`.

## First API keys

`CANOPY_API_KEYS` holds the long-lived env keys. Mint every other key with
the CLI in the image, against the instance's database:

```bash
docker compose run --rm api python scripts/create_key.py "<app>" -r
```

See [API keys](api-keys.md) for scopes, rotation and revocation.
