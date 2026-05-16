from uuid import UUID

from canopy.contracts.base import CanopyModel


class JobCreatedDTO(CanopyModel):
    """Returned by long-running operation endpoints when a job is enqueued.

    Defined in the shared kernel so future modules and the Jobs PR don't need
    to retro-wire callers when the field set evolves.
    """

    job_id: UUID
    status_url: str | None = None
