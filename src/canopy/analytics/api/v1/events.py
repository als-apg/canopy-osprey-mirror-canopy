from fastapi import Depends, Request, Response, APIRouter, status

from canopy.core.security import OptionalAPIKeyDep
from canopy.analytics.dependencies import AnalyticsServiceDep, enforce_event_size
from canopy.analytics.schemas.events import AnalyticsEventDTO

router = APIRouter(prefix="/events", tags=["Analytics / Events"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(enforce_event_size)])
async def create_event(
    event: AnalyticsEventDTO,
    request: Request,
    service: AnalyticsServiceDep,
    api_key: OptionalAPIKeyDep,
) -> Response:
    """Ingest one frontend usage/error event.

    Anonymous requests are accepted; a valid ``X-API-Key`` only enriches the
    recorded event (scope metadata). Recording is fire-and-forget — this
    endpoint never fails a client workflow over an analytics problem. The only
    rejections are a malformed body (422) and one over ``max_event_bytes`` (413).
    """
    service.record(
        event,
        api_key=api_key,
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
