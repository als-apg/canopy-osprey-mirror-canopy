import json
from uuid import uuid4
from typing import Literal, Annotated
from pathlib import Path

from fastapi import File, Form, Security, APIRouter, UploadFile
from starlette.concurrency import run_in_threadpool

from canopy.core.security import require_read_access
from canopy.contracts.jobs import JobCreatedDTO
from canopy.converter.errors import (
    UploadTooLarge,
    ConversionFailed,
    UnsupportedInput,
    BatchResultNotFound,
)
from canopy.converter.dependencies import (
    FoxValidateDep,
    ConverterConfigDep,
    ConversionServiceDep,
)
from canopy.converter.schemas.conversions import (
    BatchScreenDTO,
    UnknownWidgetDTO,
    FormulaSummaryDTO,
    ValidateResultDTO,
    ConversionStatsDTO,
    ScreenConversionResultDTO,
)
from canopy.converter.adapters.fox_validation import validate_formulas
from canopy.converter.services.conversion_service import ConversionResult

router = APIRouter(tags=["Converter / Conversions"])

_SUFFIX_KIND: dict[str, Literal["edl", "ui"]] = {".edl": "edl", ".ui": "ui"}


def _resolve_kind(kind: str | None, filename: str | None) -> Literal["edl", "ui"]:
    """Prefer the explicit ``kind`` form field; else infer from the filename suffix."""
    if kind in ("edl", "ui"):
        return kind  # type: ignore[return-value]
    if filename:
        guessed = _SUFFIX_KIND.get(Path(filename).suffix.lower())
        if guessed is not None:
            return guessed
    raise UnsupportedInput("Could not determine screen kind; pass kind=edl|ui or upload a .edl/.ui file")


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    # Reject on the declared size first so an oversized body is not fully buffered
    # into memory before the check. Content-Length can be absent or wrong, so the
    # post-read length check below stays as the authoritative backstop.
    if file.size is not None and file.size > max_bytes:
        raise UploadTooLarge(f"Upload is {file.size} bytes; limit is {max_bytes} bytes")
    data = await file.read()
    if len(data) > max_bytes:
        raise UploadTooLarge(f"Upload is {len(data)} bytes; limit is {max_bytes} bytes")
    return data


def _to_dto(result: ConversionResult, warnings: list[str]) -> ScreenConversionResultDTO:
    return ScreenConversionResultDTO(
        screen=result.screen,
        warnings=warnings,
        unknown_widgets=[
            UnknownWidgetDTO(original_class=u.original_class, count=u.count) for u in result.unknown_widgets
        ],
        formulas=[FormulaSummaryDTO(name=f.name, expression=f.expression) for f in result.formulas],
        stats=ConversionStatsDTO(
            widget_count=result.stats.widget_count,
            macro_count=result.stats.macro_count,
            rule_count=result.stats.rule_count,
        ),
    )


@router.post("/conversions", dependencies=[Security(require_read_access)])
async def create_conversion(
    service: ConversionServiceDep,
    config: ConverterConfigDep,
    fox_validate: FoxValidateDep,
    file: Annotated[UploadFile, File(description="A legacy .edl or .ui screen")],
    kind: Annotated[Literal["edl", "ui"] | None, Form()] = None,
) -> ScreenConversionResultDTO:
    """Convert one uploaded legacy screen to Canopy Screen IR (synchronous).

    ``kind`` may be omitted when the filename carries a ``.edl``/``.ui`` suffix.
    The screen is validated against the Screen IR schema before it is returned.
    """
    data = await _read_upload(file, config.max_upload_bytes)
    resolved = _resolve_kind(kind, file.filename)
    result = await run_in_threadpool(lambda: service.convert(data, kind=resolved, filename=file.filename))
    # Seam #2: validate hoisted calc:// formulas via Fox; failures become warnings.
    formula_warnings = await validate_formulas(result.formulas, validate=fox_validate)
    return _to_dto(result, result.warnings + formula_warnings)


@router.post("/conversions/validate", dependencies=[Security(require_read_access)])
async def validate_conversion(
    service: ConversionServiceDep,
    config: ConverterConfigDep,
    fox_validate: FoxValidateDep,
    file: Annotated[UploadFile, File(description="A legacy .edl or .ui screen")],
    kind: Annotated[Literal["edl", "ui"] | None, Form()] = None,
) -> ValidateResultDTO:
    """Dry-run a conversion and return only diagnostics (the IR is discarded).

    A pre-import check for the Screen Builder: ``ok`` is false when the screen
    cannot be parsed into valid IR; warnings and unknown widgets are always
    reported so the user sees what an import would surface.
    """
    data = await _read_upload(file, config.max_upload_bytes)
    # Diagnostics endpoint: an undeterminable kind (UnsupportedInput from
    # _resolve_kind or service.convert) is a "not importable" result, not an
    # error envelope, so it must fold into ok=False like a parse failure.
    try:
        resolved = _resolve_kind(kind, file.filename)
        result = await run_in_threadpool(lambda: service.convert(data, kind=resolved, filename=file.filename))
    except (UnsupportedInput, ConversionFailed) as exc:
        return ValidateResultDTO(ok=False, warnings=[exc.detail])
    formula_warnings = await validate_formulas(result.formulas, validate=fox_validate)
    return ValidateResultDTO(
        ok=True,
        warnings=result.warnings + formula_warnings,
        unknown_widgets=[
            UnknownWidgetDTO(original_class=u.original_class, count=u.count) for u in result.unknown_widgets
        ],
    )


def _looks_like_archive(data: bytes) -> bool:
    """True for a zip (``PK\\x03\\x04``) or gzip (``\\x1f\\x8b``) payload."""
    return data[:4] == b"PK\x03\x04" or data[:2] == b"\x1f\x8b"


@router.post("/conversions/batch", dependencies=[Security(require_read_access)])
async def create_batch_conversion(
    config: ConverterConfigDep,
    file: Annotated[UploadFile, File(description="A .zip or .tar.gz of legacy .edl/.ui screens")],
) -> JobCreatedDTO:
    """Convert a folder of legacy screens asynchronously.

    Accepts one archive (``.zip``/``.tar.gz``), stages it, and enqueues a job.
    Poll ``GET /jobs/v1/{jobId}``: its ``result`` is a ``BatchManifestDTO`` whose
    ``succeeded`` rows carry an ``index``; fetch each screen's IR from
    ``GET /conversions/batch/{jobId}/screens/{index}``.
    """
    # Lazy import: keeps the converter loadable without Jobs and routes the
    # cross-module call through the public gateway only (mirrors get_fox_validate).
    from canopy.jobs import gateway as jobs_gateway
    from canopy.converter.tasks import input_key

    data = await _read_upload(file, config.max_archive_bytes)
    if not _looks_like_archive(data):
        raise UnsupportedInput("Batch upload must be a .zip or .tar.gz archive")

    token = uuid4().hex
    pool = await jobs_gateway.get_pool()
    await pool.set(input_key(token), data, ex=config.batch_result_ttl_s)
    job_id = await jobs_gateway.enqueue("convert_batch", token)
    return JobCreatedDTO(job_id=job_id, status_url=f"/jobs/v1/{job_id}")


@router.get("/conversions/batch/{job_id}/screens/{index}", dependencies=[Security(require_read_access)])
async def get_batch_screen(job_id: str, index: int) -> BatchScreenDTO:
    """Fetch one converted screen's IR from a finished batch by its manifest ``index``.

    Diagnostics (warnings, unknown widgets) live in the batch manifest row; this
    returns just the Screen IR document the editor loads.
    """
    from canopy.jobs import gateway as jobs_gateway
    from canopy.converter.tasks import result_key

    pool = await jobs_gateway.get_pool()
    raw = await pool.get(result_key(job_id, index))
    if raw is None:
        raise BatchResultNotFound(f"No batch screen for job {job_id!r} index {index} (expired or unknown)")
    return BatchScreenDTO(screen=json.loads(raw))
