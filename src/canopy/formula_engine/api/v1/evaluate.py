from fastapi import Security, APIRouter

from canopy.core.security import require_read_access
from canopy.formula_engine.dependencies import FormulaServiceDep
from canopy.formula_engine.schemas.formula import FormulaResultDTO, EvaluateRequestDTO

router = APIRouter(tags=["Formula Engine / Evaluate"])


@router.post("/evaluate", dependencies=[Security(require_read_access)])
async def evaluate(body: EvaluateRequestDTO, service: FormulaServiceDep) -> FormulaResultDTO:
    """Evaluate a formula over a time window.

    Returns a discriminated union by ``kind`` (scalar / timeseries / waveform).
    """
    result = await service.evaluate(body.spec, t0_ns=body.t0_ns, t1_ns=body.t1_ns)
    return FormulaResultDTO(**result.model_dump())
