from fastapi import Security, APIRouter

from canopy.jobs import gateway
from canopy.jobs.errors import JobNotFound
from canopy.core.security import require_read_access
from canopy.jobs.schemas.job import JobStatusDTO

router = APIRouter(tags=["Jobs"])


@router.get("/{job_id}", dependencies=[Security(require_read_access)])
async def get_job(job_id: str) -> JobStatusDTO:
    """Poll the status / result of a background job by its id."""
    dto = await gateway.get_status(job_id)
    if dto is None:
        raise JobNotFound(f"Job {job_id!r} not found")
    return dto
