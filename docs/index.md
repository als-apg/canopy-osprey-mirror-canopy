---
hide:
    - toc
    - footer
---

# Canopy

Canopy is a backend platform for making control system tools and applications

New here? Start with the [Getting started guide](getting_started.md) — clone
to live PV data in a few minutes, no EPICS required.

## Modules

- **Hare** — live-PV broker: WebSocket streaming, REST reads and writes.
- **Mole** — archiver-appliance proxy for historical PV data.
- **Raccoon** — PV name lookup over the lab directory service.
- **Vole** — time-aligned in-memory buffer merging live and historical data.
- **Fox** — formula engine over live and historical PV data.
- **Squirrel** — machine-state snapshot save / restore / compare.
- **Jobs** — background-job status over the Arq + Redis worker pool.
- **Converter** — EDM `.edl` / Qt `.ui` to Canopy Screen IR.

Planned: **Owl** (plot-config persistence + legacy importer) and **Wren**
(rules engine, hosted in Hare).

See the [modules overview](modules/index.md) for shared conventions
(authentication, errors, health checks).

## Running it

- [Getting started](getting_started.md) — local development, simulation and
  real EPICS backends.
- [Deployment](deployment/index.md) — Docker Compose and deploying Canopy at
  your facility.
