# Canopy Deployment

Deployment docs live in the documentation site:

- [Deploy at your facility](https://slaclab.github.io/canopy/deployment/production/)
  ([source](docs/deployment/production.md)): image tags, required
  configuration, Postgres and Redis, migrations, and health checks.
- [Deployment overview](https://slaclab.github.io/canopy/deployment/)
  ([source](docs/deployment/)): configuration reference,
  [API keys](docs/deployment/api-keys.md), and
  [Docker Compose](docs/deployment/docker.md).

The reference production compose file is
[`docker/docker-compose.prod.yml`](docker/docker-compose.prod.yml).

SLAC deploys Canopy from an internal repository; this repository holds only
the generic pieces: the image, the reference compose file, and these docs.
