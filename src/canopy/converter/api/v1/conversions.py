from typing import Literal, Annotated
from pathlib import Path

from fastapi import File, Form, Security, APIRouter, UploadFile
from starlette.concurrency import run_in_threadpool

from canopy.core.security import require_read_access
from canopy.converter.errors import UploadTooLarge, ConversionFailed, UnsupportedInput
from canopy.converter.dependencies import (
    FoxValidateDep,
    ConverterConfigDep,
    ConversionServiceDep,
)
from canopy.converter.schemas.conversions import (
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
    result = await run_in_threadpool(lambda: service.convert(data, kind=resolved))
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
        result = await run_in_threadpool(lambda: service.convert(data, kind=resolved))
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
