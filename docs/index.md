# Canopy

Canopy is the SLAC EED Software Applications Group's backend for a modern control-system display platform — a clean-break replacement for PyDM 1.0.

This repository hosts the Python backend (FastAPI modular monolith). The frontend (React + Tauri + Carbon) lives in a separate repository.

## Modules

- **Hare** — live PV WebSocket broker (hosts the Wren rules engine).
- **Mole** — archiver-appliance proxy.
- **Fox** — formula engine.
- **Vole** — time-aligned in-memory buffer.
- **Squirrel** — snapshot / restore / compare.
- **Owl** — plot-config persistence + legacy importer.
- **Jobs** — Arq + Redis worker pool.
- **EPICS Connector** — low-level CA / PVA layer.

See the [GitHub wiki](https://github.com/slaclab/Canopy/wiki) for module designs.
