"""Pydantic schemas for GET /transactions."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TransactionStatus, TransactionSortField, SortOrder
from app.utils.pagination import PaginatedResponse


# ── Response item ─────────────────────────────────────────────────────────────


class TransactionItem(BaseModel):
    """A single transaction in the listing response.

    Includes ``merchant_name`` denormalised from the merchants table so
    callers do not need a second request.
    """

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str          # joined from merchants table
    amount: Decimal
    currency: str
    current_status: TransactionStatus
    initiated_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Paginated list ────────────────────────────────────────────────────────────

TransactionListResponse = PaginatedResponse[TransactionItem]


# ── Query filter params (used by the service, validated by the router) ────────


class TransactionFilters(BaseModel):
    """Validated filter parameters for the transaction listing query."""

    merchant_id: Optional[str] = Field(None, description="Filter by exact merchant ID")
    current_status: Optional[TransactionStatus] = Field(None, description="Filter by transaction status")
    start_date: Optional[datetime] = Field(None, description="Filter transactions initiated on or after this datetime (inclusive)")
    end_date: Optional[datetime] = Field(None, description="Filter transactions initiated on or before this datetime (inclusive)")
    sort_by: TransactionSortField = Field(
        TransactionSortField.INITIATED_AT,
        description="Column to sort by. Whitelisted to prevent SQL injection.",
    )
    order: SortOrder = Field(
        SortOrder.DESC,
        description="Sort direction — asc or desc.",
    )


# ── Detail endpoint schemas ────────────────────────────────────────────────────


class EventHistoryItem(BaseModel):
    """A single event in a transaction's history, ordered by event_timestamp ASC."""

    event_id: uuid.UUID
    event_type: str
    amount: Decimal
    currency: str
    event_timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class TransactionDetailResponse(BaseModel):
    """Full transaction detail including complete event history."""

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    amount: Decimal
    currency: str
    current_status: TransactionStatus
    initiated_at: datetime
    updated_at: datetime
    events: list[EventHistoryItem]

    model_config = ConfigDict(from_attributes=True)

