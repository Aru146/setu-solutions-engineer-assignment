"""Router for POST /events — payment event ingestion."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.logging_config import get_logger
from app.schemas.event import EventIngestRequest, EventIngestResponse, EventData, TransactionSnapshot
from app.services import event_service

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=EventIngestResponse,
    responses={
        201: {"description": "Event ingested successfully"},
        200: {"description": "Duplicate event — idempotent response (returned via response_model)"},
        422: {"description": "Validation error — invalid payload"},
        500: {"description": "Unexpected server error"},
    },
    summary="Ingest a payment lifecycle event",
    description=(
        "Accepts a payment lifecycle event and updates the transaction state. "
        "Submission is idempotent: sending the same `event_id` more than once "
        "returns the original event with HTTP 200 and makes no state changes."
    ),
)
def ingest_event(
    payload: EventIngestRequest,
    response_obj: Response,
    db: Session = Depends(get_db),
) -> EventIngestResponse:
    """Ingest a single payment event.

    - **Idempotent**: duplicate ``event_id`` → 200 OK, no state mutation.
    - **Atomic**: merchant + transaction + event writes commit together.
    - **Consistent**: ``current_status`` always reflects the latest event.
    """
    try:
        result = event_service.ingest_event(db, payload)
    except IntegrityError as exc:
        logger.error("Unrecoverable IntegrityError: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Event violates a data integrity constraint. Check the payload.",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error during event ingest: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please retry.",
        ) from exc

    if result.is_duplicate:
        # Override the default 201 to 200 for duplicate events.
        response_obj.status_code = status.HTTP_200_OK

    return EventIngestResponse(
        success=True,
        message="Event already ingested" if result.is_duplicate else "Event ingested successfully",
        event=EventData.model_validate(result.event),
        transaction=TransactionSnapshot.model_validate(result.transaction),
    )
