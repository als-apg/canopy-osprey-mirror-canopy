from canopy.contracts.base import CanopyModel


class JobCreatedDTO(CanopyModel):
    """Returned by long-running operation endpoints when a job is enqueued.

    Defined in the shared kernel so future modules don't need to retro-wire callers
    when the field set evolves. ``job_id`` is a string because Arq job ids are
    opaque strings (e.g. a uuid4 hex), matching ``JobStatusDTO.id``.
    """

    job_id: str
    status_url: str | None = None
