"""Arq worker tasks for the converter module.

Batch (folder) conversion runs off the request path: the API stages an uploaded
archive in Redis and enqueues :func:`convert_batch`, which the ``canopy-worker``
process runs. Each archive entry is converted independently; the per-screen IR is
written to its own Redis key and the task returns a lightweight *manifest* (the
Arq job result) so a thousand-screen folder never lands as one giant blob.

The worker process does not run ``converter_lifespan`` (that binds the gateway
service in the API process), so this module builds and memoizes its own
``ConversionService`` from config.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import zipfile
from typing import Any
from pathlib import PurePosixPath

import anyio

from canopy.converter.config import ConverterConfig, get_converter_config
from canopy.converter.errors import ConverterError
from canopy.converter.adapters.edm_env import apply_edm_colors
from canopy.converter.adapters.beaver_registry import build_registry
from canopy.converter.services.conversion_service import ConversionService

logger = logging.getLogger(__name__)

# Redis key namespaces. The input key is written by the API route before
# enqueuing; result keys are written here and fetched by the screen route.
INPUT_KEY = "canopy:converter:input:{token}"
RESULT_KEY = "canopy:converter:result:{job_id}:{index}"

_SUFFIX_KIND = {".edl": "edl", ".ui": "ui"}

_service: ConversionService | None = None


def _get_service() -> ConversionService:
    """Build (once per worker process) the ConversionService used for batches."""
    global _service
    if _service is None:
        config = get_converter_config()
        apply_edm_colors(config)
        _service = ConversionService(registry=build_registry(config))
    return _service


def input_key(token: str) -> str:
    return INPUT_KEY.format(token=token)


def result_key(job_id: str, index: int) -> str:
    return RESULT_KEY.format(job_id=job_id, index=index)


def _kind_for(name: str) -> str | None:
    return _SUFFIX_KIND.get(PurePosixPath(name).suffix.lower())


def _is_safe_path(name: str) -> bool:
    """Reject absolute paths and traversal (zip-slip); entries must stay relative."""
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts


def _iter_archive(data: bytes) -> list[tuple[str, bytes]]:
    """Yield ``(path, bytes)`` for each regular file in a zip or tar.gz archive.

    Type is sniffed from magic bytes (zip ``PK\\x03\\x04``, gzip ``\\x1f\\x8b``).
    Directories, symlinks and unsafe paths are dropped here; ``convert_batch``
    separately records non-screen entries as skipped.
    """
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return [
                (info.filename, zf.read(info))
                for info in zf.infolist()
                if not info.is_dir() and _is_safe_path(info.filename)
            ]
    if data[:2] == b"\x1f\x8b" or tarfile.is_tarfile(io.BytesIO(data)):
        out: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not _is_safe_path(member.name):
                    continue
                fh = tf.extractfile(member)
                if fh is not None:
                    out.append((member.name, fh.read()))
        return out
    raise ConverterError("Unrecognized archive format; expected .zip or .tar.gz")


async def convert_batch(ctx: dict, token: str) -> dict[str, Any]:
    """Convert every screen in a staged archive; return a manifest (the job result).

    ``ctx`` is supplied by Arq: ``ctx["redis"]`` is the pool, ``ctx["job_id"]`` is
    this job's id (used to namespace per-screen result keys).
    """
    redis = ctx["redis"]
    job_id = ctx["job_id"]
    config = get_converter_config()

    data = await redis.get(input_key(token))
    if data is None:
        # The staged archive expired (or was never written). Report rather than crash.
        logger.warning("convert_batch %s: input archive %s missing/expired", job_id, token)
        return _manifest([])

    try:
        entries = _iter_archive(data)
        service = _get_service()
        rows: list[dict[str, Any]] = []
        total = len(entries)
        for i, (path, raw) in enumerate(entries):
            if i >= config.max_batch_entries:
                rows.append({"path": path, "status": "skipped", "warnings": ["batch entry cap reached"]})
                continue
            rows.append(await _convert_entry(redis, service, job_id, i, path, raw, config))
            if total and (i % 25 == 0 or i == total - 1):
                await _report(ctx, job_id, int((i + 1) / total * 100), f"{i + 1}/{total}")
        return _manifest(rows)
    finally:
        await redis.delete(input_key(token))


async def _convert_entry(
    redis: Any,
    service: ConversionService,
    job_id: str,
    index: int,
    path: str,
    raw: bytes,
    config: ConverterConfig,
) -> dict[str, Any]:
    """Convert one archive entry into a manifest row, isolating its failures."""
    kind = _kind_for(path)
    if kind is None:
        return {"path": path, "status": "skipped", "warnings": ["not a .edl/.ui screen"]}
    try:
        result = await anyio.to_thread.run_sync(lambda: service.convert(raw, kind=kind))
    except ConverterError as exc:
        return {"path": path, "status": "failed", "warnings": [exc.detail]}

    key = result_key(job_id, index)
    await redis.set(key, json.dumps(result.screen), ex=config.batch_result_ttl_s)
    return {
        "path": path,
        "status": "succeeded",
        "index": index,
        "warnings": result.warnings,
        "unknownWidgets": [{"originalClass": u.original_class, "count": u.count} for u in result.unknown_widgets],
        "resultKey": key,
    }


def _manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll rows up into the camelCase manifest returned as the Arq job result."""
    counts = {"succeeded": 0, "failed": 0, "skipped": 0}
    for row in rows:
        counts[row["status"]] += 1
    return {"total": len(rows), **counts, "rows": rows}


async def _report(ctx: dict, job_id: str, percent: int, message: str) -> None:
    # Lazy import keeps the converter loadable without Jobs and respects the
    # gateway-only boundary (mirrors the route).
    from canopy.jobs import gateway as jobs_gateway

    await jobs_gateway.report_progress(ctx["redis"], job_id, percent, message)
