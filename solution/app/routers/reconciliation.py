"""Router for GET /reconciliation endpoints (summary and discrepancies)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.logging_config import get_logger
from app.models.enums import SummaryGroupBy
from app.schemas.reconciliation import (
    DiscrepancyEventItem,
    DiscrepancyItem,
    MerchantSummary,
    ReconciliationDiscrepanciesResponse,
    ReconciliationSummaryResponse,
    SummaryGroup,
)
from app.services import discrepancy_service, reconciliation_service

logger = get_logger(__name__)

router = APIRouter(prefix="/reconciliation", tags=["Reconciliation"])


@router.get(
    "/summary",
    response_model=ReconciliationSummaryResponse,
    summary="Reconciliation summary grouped by merchant, date, or status",
    description=(
        "Returns aggregated transaction statistics grouped by the requested "
        "dimension:\n\n"
        "- ``group_by=merchant`` (default) — one row per merchant. Response "
        "also includes the legacy ``merchants`` / ``total_merchants`` fields "
        "for backward compatibility.\n"
        "- ``group_by=date`` — one row per UTC calendar day of ``initiated_at``.\n"
        "- ``group_by=status`` — one row per lifecycle state.\n\n"
        "The ``groups`` array has a uniform shape across all group_by modes."
    ),
)
def get_reconciliation_summary(
    group_by: SummaryGroupBy = Query(
        default=SummaryGroupBy.MERCHANT,
        description="Grouping dimension: merchant | date | status. Default: merchant.",
    ),
    db: Session = Depends(get_db),
) -> ReconciliationSummaryResponse:
    """Return transaction counts grouped by the requested dimension."""
    result = reconciliation_service.get_reconciliation_summary(db, group_by=group_by)

    groups = [SummaryGroup.model_validate(g, from_attributes=True) for g in result.groups]

    # Backward-compat: also emit merchants/total_merchants when grouping by merchant.
    merchants_payload = None
    total_merchants = None
    if result.group_by == SummaryGroupBy.MERCHANT:
        merchants_payload = [
            MerchantSummary.model_validate(m, from_attributes=True) for m in result.merchants
        ]
        total_merchants = len(merchants_payload)

    return ReconciliationSummaryResponse(
        group_by=result.group_by,
        groups=groups,
        total_groups=len(groups),
        merchants=merchants_payload,
        total_merchants=total_merchants,
    )


@router.get(
    "/discrepancies",
    response_model=ReconciliationDiscrepanciesResponse,
    summary="Reconciliation discrepancy report",
    description=(
        "Scans all transactions for reconciliation anomalies. "
        "Returns one entry per detected anomaly — a single transaction may appear "
        "multiple times if it has multiple distinct discrepancy types. "
        "Event history within each entry is ordered by event_timestamp ASC.\n\n"
        "The ``stale_after_hours`` query parameter controls the ``processed_never_settled`` "
        "check: transactions in ``payment_processed`` state older than the given number "
        "of hours (based on the latest event timestamp) are flagged as stale. Default 24."
    ),
)
def get_reconciliation_discrepancies(
    stale_after_hours: int = Query(
        default=24,
        ge=0,
        le=24 * 365,  # up to a year
        description=(
            "Age threshold in hours for the processed_never_settled check. "
            "A payment_processed transaction whose most recent event is older than "
            "this many hours is flagged as stale. Set to 0 to flag ALL processed-only "
            "transactions regardless of age."
        ),
    ),
    db: Session = Depends(get_db),
) -> ReconciliationDiscrepanciesResponse:
    """Return all detected reconciliation anomalies across all transactions."""
    result = discrepancy_service.get_discrepancies(
        db,
        processed_stale_after_hours=stale_after_hours,
    )
    return ReconciliationDiscrepanciesResponse(
        discrepancies=[
            DiscrepancyItem(
                transaction_id=d.transaction_id,
                merchant_id=d.merchant_id,
                merchant_name=d.merchant_name,
                current_status=d.current_status,
                discrepancy_type=d.discrepancy_type,
                explanation=d.explanation,
                event_history=[
                    DiscrepancyEventItem(
                        event_id=e.event_id,
                        event_type=e.event_type,
                        amount=e.amount,
                        currency=e.currency,
                        event_timestamp=e.event_timestamp,
                    )
                    for e in d.event_history
                ],
            )
            for d in result.discrepancies
        ],
        total_discrepancies=len(result.discrepancies),
    )
