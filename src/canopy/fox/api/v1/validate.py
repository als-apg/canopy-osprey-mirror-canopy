from fastapi import Security, APIRouter

from canopy.core.security import require_read_access
from canopy.fox.dependencies import FormulaServiceDep
from canopy.fox.schemas.formula import FormulaValidationDTO
from canopy.contracts.formula import FormulaSpec

router = APIRouter(tags=["Fox / Validate"])


@router.post("/validate", dependencies=[Security(require_read_access)])
async def validate(body: FormulaSpec, service: FormulaServiceDep) -> FormulaValidationDTO:
    """Parse and typecheck a formula without evaluating.

    Useful for the formula-maker UI to red-line bad expressions as the user types.
    """
    result = await service.validate(body)
    return FormulaValidationDTO(**result.model_dump())
