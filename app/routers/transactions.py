"""Router for GET /transactions — transaction listing and single-transaction detail."""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.logging_config import get_logger
from app.models.enums import TransactionStatus, TransactionSortField, SortOrder
from app.schemas.transaction import (
    TransactionDetailResponse,
    TransactionFilters,
    TransactionItem,
    TransactionListResponse,
    EventHistoryItem,
)
from app.services import transaction_service
from app.utils.pagination import PaginationParams

logger = get_logger(__name__)

router = APIRouter(prefix="/transactions", tags=["Transactions"])


@router.get(
    "",
    response_model=TransactionListResponse,
    summary="List transactions",
    description=(
        "Returns a paginated list of transactions, optionally filtered by merchant, "
        "status, and date range. Sortable on multiple columns; default is "
        "``initiated_at DESC``."
    ),
)
def list_transactions(
    # ── Filter params ────────────────────────────────────────────────
    merchant_id: Optional[str] = Query(
        default=None,
        description="Filter by exact merchant ID (e.g. 'merchant_1')",
    ),
    current_status: Optional[TransactionStatus] = Query(
        default=None,
        description="Filter by transaction status",
    ),
    start_date: Optional[datetime] = Query(
        default=None,
        description="Return transactions with initiated_at >= this value (ISO 8601)",
    ),
    end_date: Optional[datetime] = Query(
        default=None,
        description="Return transactions with initiated_at <= this value (ISO 8601)",
    ),
    # ── Sorting ──────────────────────────────────────────────────────
    sort_by: TransactionSortField = Query(
        default=TransactionSortField.INITIATED_AT,
        description=(
            "Column to sort by. Allowed: initiated_at, updated_at, amount, "
            "merchant_id. Values outside this whitelist are rejected with 422."
        ),
    ),
    order: SortOrder = Query(
        default=SortOrder.DESC,
        description="Sort direction — asc or desc. Default: desc.",
    ),
    # ── Pagination ───────────────────────────────────────────────────
    pagination: PaginationParams = Depends(),
    # ── Infrastructure ───────────────────────────────────────────────
    db: Session = Depends(get_db),
) -> TransactionListResponse:
    """List transactions with optional filters, sorting, and offset pagination."""
    filters = TransactionFilters(
        merchant_id=merchant_id,
        current_status=current_status,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        order=order,
    )

    result = transaction_service.list_transactions(db, filters, pagination)

    return TransactionListResponse(
        items=[TransactionItem.model_validate(row, from_attributes=True) for row in result.items],
        total=result.total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{transaction_id}",
    response_model=TransactionDetailResponse,
    responses={
        200: {"description": "Transaction found"},
        404: {"description": "Transaction not found"},
    },
    summary="Get transaction detail",
    description=(
        "Returns a single transaction with its complete event history ordered "
        "by ``event_timestamp`` ascending."
    ),
)
def get_transaction(
    transaction_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> TransactionDetailResponse:
    """Fetch a single transaction with full event history.

    Returns **404** if the transaction does not exist.
    """
    result = transaction_service.get_transaction_by_id(db, transaction_id)

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction '{transaction_id}' not found.",
        )

    return TransactionDetailResponse(
        transaction_id=result.transaction_id,
        merchant_id=result.merchant_id,
        merchant_name=result.merchant_name,
        amount=result.amount,
        currency=result.currency,
        current_status=result.current_status,
        initiated_at=result.initiated_at,
        updated_at=result.updated_at,
        events=[
            EventHistoryItem.model_validate(e, from_attributes=True)
            for e in result.events
        ],
    )
