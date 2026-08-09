"""Pydantic schemas for GET /reconciliation endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SummaryGroupBy


# ── Per-merchant summary (backward-compat) ────────────────────────────────────


class MerchantSummary(BaseModel):
    """Aggregated transaction statistics for a single merchant."""

    merchant_id: str
    merchant_name: str
    total_transactions: int = Field(..., description="Total transactions across all statuses")
    payment_initiated_count: int = Field(..., description="Transactions currently in payment_initiated state")
    payment_processed_count: int = Field(..., description="Transactions currently in payment_processed state")
    settled_count: int = Field(..., description="Transactions successfully settled")
    failed_count: int = Field(..., description="Transactions that failed")

    model_config = ConfigDict(from_attributes=True)


# ── Generic group summary (new — one shape per group_by mode) ─────────────────


class SummaryGroup(BaseModel):
    """A single aggregation group entry.

    ``group_key`` carries the value of the grouping dimension:
    - merchant grouping → merchant_id (e.g. ``"merchant_1"``)
    - date grouping → ISO-8601 date string (e.g. ``"2026-01-10"``)
    - status grouping → status name (e.g. ``"settled"``)

    ``merchant_name`` is only populated when grouping by merchant.
    """

    group_key: str = Field(..., description="Value of the grouping dimension for this row")
    merchant_name: Optional[str] = Field(
        None, description="Populated only when group_by=merchant"
    )
    total_transactions: int
    payment_initiated_count: int
    payment_processed_count: int
    settled_count: int
    failed_count: int

    model_config = ConfigDict(from_attributes=True)


# ── Summary response ──────────────────────────────────────────────────────────


class ReconciliationSummaryResponse(BaseModel):
    """Top-level reconciliation summary response.

    The response shape is stable across group_by values: callers can always
    read ``groups`` regardless of dimension. For backward compatibility with
    the earlier API, ``merchants`` and ``total_merchants`` are also emitted
    when ``group_by == merchant``.
    """

    group_by: SummaryGroupBy = Field(
        ..., description="Grouping dimension used for this response"
    )
    groups: list[SummaryGroup] = Field(
        ..., description="One entry per group value (uniform across group_by modes)"
    )
    total_groups: int = Field(..., description="Number of distinct group values")

    # ── Backward-compat fields (only populated when group_by=merchant) ──
    merchants: Optional[list[MerchantSummary]] = Field(
        None,
        description=(
            "Original response shape — populated only when group_by=merchant. "
            "Prefer ``groups`` for new integrations."
        ),
    )
    total_merchants: Optional[int] = Field(
        None,
        description="Original response shape — populated only when group_by=merchant.",
    )


# ── Discrepancy endpoint schemas ──────────────────────────────────────────────


class DiscrepancyEventItem(BaseModel):
    """A single event entry inside a discrepancy's event history."""

    event_id: uuid.UUID
    event_type: str
    amount: Decimal
    currency: str
    event_timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscrepancyItem(BaseModel):
    """A single detected reconciliation anomaly for one transaction."""

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    current_status: str
    discrepancy_type: str = Field(..., description=(
        "One of: duplicate_event_type | missing_payment_initiated | "
        "payment_failed_after_settled | settled_without_payment_processed | "
        "processed_never_settled | invalid_state_transition"
    ))
    explanation: str = Field(..., description="Human-readable description of the anomaly")
    event_history: list[DiscrepancyEventItem] = Field(
        ..., description="Complete event history for this transaction ordered by event_timestamp ASC"
    )

    model_config = ConfigDict(from_attributes=True)


class ReconciliationDiscrepanciesResponse(BaseModel):
    """Top-level discrepancy report response."""

    discrepancies: list[DiscrepancyItem]
    total_discrepancies: int = Field(
        ..., description="Total number of anomaly entries (one transaction may contribute multiple)"
    )
