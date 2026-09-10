# Facility deployment

Canopy deploys to SLAC servers via the reusable
`slaclab/software-factory/.github/workflows/container-deploy.yml` workflow,
the same machinery `react-squirrel-backend` uses. Deploys run on self-hosted
runners, pull the image from GHCR, render a per-instance `.env`, bring up
`docker/docker-compose.prod.yml` (api + worker only), and health-check
`/health`.

The data layer (postgres + redis) is **not** in the app compose. It is
brought up once per instance by the `Init Data Layer (one-time)` workflow and
reached over the host's `127.0.0.1:<port>` (app containers run
`network_mode: host` for EPICS CA/PVA UDP).

## Instances

| Instance | Facility (inventory)  | API port | PG host port | Redis host port | DB          | Deploy component |
|----------|-----------------------|----------|--------------|-----------------|-------------|------------------|
| NC       | CONTAINER_LCLS        | 8080     | 5442         | 6389            | canopy_nc   | canopy-nc        |
| SC       | CONTAINER_LCLS        | 8081     | 5443         | 6390            | canopy_sc   | canopy-sc        |
| NLCTA    | CONTAINER_TESTFAC     | 8080     | 5442         | 6389            | canopy_nlcta| canopy-nlcta     |
| ASTA     | CONTAINER_TESTFAC     | 8081     | 5443         | 6390            | canopy_asta | canopy-asta      |
| FACET    | CONTAINER_FACET       | 8080     | 5442         | 6389            | canopy_facet| canopy-facet     |
| DEV      | CONTAINER_DEV         | 8080     | 5442         | 6389            | canopy_dev  | canopy           |

All per-instance app config lives in the `APP_SECRETS_<INSTANCE>` GitHub
secret — credentials *and* plain config (API port, EPICS address lists, debug
flag). Nothing is committed to the repo, deliberately: the EPICS
`*_ADDR_LIST` values are internal network addresses. The only committed
per-instance data is the non-secret data-layer container/volume/port map in
`docker/data-layer/instances.yml`.

Canopy shares each facility host with the legacy react-squirrel data layer
(which publishes 5432/6379, and 5433/6380 for the second instance on a box),
so Canopy's data layer is offset by +10: 5442/6389 for the first instance on
a host, 5443/6390 for the second. These host ports must match the ones
embedded in each secret's `CANOPY_DATABASE_URL` / `CANOPY_REDIS_URL`.

## Prerequisite: `APP_SECRETS_<INSTANCE>` GitHub secrets

Each instance needs a repo secret `APP_SECRETS_<INSTANCE>`
(`APP_SECRETS_NC`, `APP_SECRETS_SC`, `APP_SECRETS_NLCTA`, `APP_SECRETS_ASTA`,
`APP_SECRETS_FACET`, `APP_SECRETS_DEV`), a **JSON object** keyed by facility.
At deploy time software-factory's `deploy.sh` merges the facility block over
`_shared` (facility wins) and writes every key of the result as a line in the
app's `.env` — the secret is the **only** source of the app's environment.

Template (replace every `<...>` placeholder; EPICS address lists come from the
facility's network config and are intentionally not written down in this
repo):

```json
{
  "_shared": {
    "CANOPY_API_KEYS": "<read-write-token>:read+write,<read-only-token>:read"
  },
  "CONTAINER_LCLS": {
    "API_PORT": 8080,
    "CANOPY_DEBUG": false,
    "CANOPY_DATABASE_POOL_SIZE": 30,
    "CANOPY_DATABASE_MAX_OVERFLOW": 20,
    "POSTGRES_PASSWORD": "<pg-password>",
    "REDIS_PASSWORD": "<redis-password>",
    "CANOPY_DATABASE_URL": "postgresql+asyncpg://canopy:<pg-password>@127.0.0.1:5442/canopy_nc",
    "CANOPY_REDIS_URL": "redis://:<redis-password>@127.0.0.1:6389/0",
    "EPICS_CA_ADDR_LIST": "<facility CA address list>",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_SERVER_PORT": 5064,
    "EPICS_CA_REPEATER_PORT": 5065,
    "EPICS_PVA_ADDR_LIST": "<facility PVA address list>",
    "EPICS_PVA_AUTO_ADDR_LIST": "YES",
    "EPICS_PVA_SERVER_PORT": 5075,
    "EPICS_PVA_BROADCAST_PORT": 5076
  }
}
```

Per-instance adjustments to the template:

- `API_PORT`, the DB/Redis host ports in the URLs, and the DB name follow the
  instance table above (SC and ASTA are the `8081` / `5443` / `6390`
  instances; every other instance uses `5442` / `6389`).
- DEV: `CANOPY_DEBUG: true`; DEV and FACET typically only set
  `EPICS_CA_ADDR_LIST`.
- NLCTA / ASTA: TESTFAC runs non-default CA server/repeater ports and
  explicit PVA address/broadcast settings — take the values from the facility
  EPICS environment.
- The facility key must match the instance's facility (`CONTAINER_LCLS`,
  `CONTAINER_TESTFAC`, `CONTAINER_FACET`, `CONTAINER_DEV`).

Rules the workflows enforce or that keep logs sane:

- Passwords must match the ones embedded in the URLs (init verifies this;
  deploy and init both reject a `KEY=value`/dotenv blob up front, rather than
  dying with an opaque `jq` error later).
- `API_PORT`, `CANOPY_DATABASE_URL`, and `CANOPY_REDIS_URL` are required
  (deploy validates the merged facility block): without `API_PORT`,
  `deploy.sh` silently assumes 8080, which breaks the 8081 instances.
- Values may be JSON numbers/booleans (rendered as `KEY=8081`, `KEY=false`).
  Prefer them for short or common values: the deploy workflow log-masks every
  string value longer than 4 characters, so `"CANOPY_DEBUG": "false"`
  (string) would redact the word `false` throughout the deploy logs.
- Values must be single-line (each key becomes one `KEY=value` line in
  `.env`).

## Data layer init (one-time per instance)

Actions -> `Init Data Layer (one-time)` -> Run workflow.

| Input              | Notes                                                              |
|--------------------|--------------------------------------------------------------------|
| `instance`         | NC / SC / NLCTA / ASTA / FACET / DEV                               |
| `confirm_instance` | Must be typed identically to `instance`                            |
| `mode`             | `init` fresh bring-up (refuses if state exists); `recreate` restart existing containers against the same volume (no data loss); `wipe` pg_dump then destroy + rebuild |
| `dry_run`          | `true` runs ansible with `--check`                                 |
| `backup_root`      | Default `/sdf/group/ad/eed/ad-build/canopy-backups`                |

Safeguards: `init` refuses if the postgres/redis container or the postgres
volume already exists; `wipe` takes a verified `pg_dump | gzip` backup first
(and refuses if the volume exists but its container is gone);
`confirm_instance` must match `instance`; a per-instance concurrency lock
(shared with the deploy workflows) prevents init from overlapping a deploy of
the same instance.

## Deploy

### Automatic (DEV)

Every push to `main` builds + pushes the image (`build-image.yml`, tagged
with the commit's short SHA and `latest`) and then deploys it to DEV via the
appended `deploy-dev` job, running `alembic upgrade head` before the services
restart.

### Manual (any instance)

Actions -> `Deploy` -> Run workflow.

| Input               | Notes                                                          |
|---------------------|----------------------------------------------------------------|
| `image_tag`         | GHCR tag — the bare commit short SHA from a main build (e.g. `abc1234`) |
| `deploy_<instance>` | One checkbox per instance (nc/sc/nlcta/asta/facet/dev)         |
| `migration_command` | Default `alembic upgrade head`, run as `compose run --rm api <command>` before services restart. Clear it to skip — e.g. when rolling back to an older image whose migration tree predates the database revision |
| `force_deploy`      | Deploy even if the same tag is already running                 |
| `dry_run`           | Ansible `--check`, no changes                                  |

Each selected instance runs `container-deploy.yml` with its
`component_name`, facility, and `APP_SECRETS_<INSTANCE>`. A deploy to LCLS /
FACET / TESTFAC also posts an ELOG entry (DEV does not).

## Verify a deploy

```bash
# On the target host:
docker ps
curl http://localhost:<API_PORT>/health   # 8080 or 8081 per the instance table
```

## Troubleshooting

```bash
# App logs:
docker logs canopy-<instance>-api --tail 50     # container name follows compose project

# Data layer health:
docker exec canopy-<instance>-postgres pg_isready -U canopy -d canopy_<instance>
docker exec canopy-<instance>-redis redis-cli -a <REDIS_PASSWORD> ping

# Current deployed tag (written by the playbook):
cat /sdf/group/ad/eed/ad-build/canopy/.current_tag   # DEV; suffix -<instance> for others
```

## Notes

- Init Data Layer creates an **empty** database; the schema comes entirely
  from the deploy-time `alembic upgrade head` (the app never creates tables
  at runtime), so the first deploy after an init must not skip migrations.
- App containers use `network_mode: host` (EPICS CA/PVA needs UDP broadcast)
  and reach postgres/redis via `127.0.0.1:<host port>`; there is no shared
  docker network.
- The local `docker/docker-compose.yml` (bundled postgres + redis) is for
  development only. Production uses `docker/docker-compose.prod.yml`, which
  the playbook copies.
- Self-hosted runners, `global_inventory.ini`, and `deploy.sh` live in
  software-factory and are shared with the other SLAC container apps.
