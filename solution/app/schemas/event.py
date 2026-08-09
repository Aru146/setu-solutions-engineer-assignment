"""Pydantic schemas for POST /events.

Separates the wire format (what the client sends) from the ORM model,
allowing independent evolution of both.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EventType, TransactionStatus


# ── Request ───────────────────────────────────────────────────────────────────


class EventIngestRequest(BaseModel):
    """Incoming payload for POST /events.

    Field names match the sample_events.json format exactly.
    ``timestamp`` is the business clock from the source system and maps to
    ``event_timestamp`` in the database.
    """

    event_id: uuid.UUID = Field(..., description="UUID from the external payment system — idempotency key")
    event_type: EventType = Field(..., description="One of: payment_initiated, payment_processed, payment_failed, settled")
    transaction_id: uuid.UUID = Field(..., description="UUID of the transaction this event belongs to")
    merchant_id: str = Field(..., max_length=50, description="External merchant identifier")
    merchant_name: str = Field(..., max_length=255, description="Human-readable merchant name")
    amount: Decimal = Field(..., gt=0, description="Transaction amount — must be positive")
    currency: str = Field(default="INR", max_length=3, description="ISO 4217 currency code")
    timestamp: datetime = Field(..., description="Business timestamp from the source payment system")

    model_config = ConfigDict(str_strip_whitespace=True)


# ── Response ──────────────────────────────────────────────────────────────────


class EventData(BaseModel):
    """Serialized representation of a persisted event."""

    event_id: uuid.UUID
    event_type: EventType
    transaction_id: uuid.UUID
    merchant_id: str
    amount: Decimal
    currency: str
    event_timestamp: datetime
    ingested_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TransactionSnapshot(BaseModel):
    """Snapshot of the transaction state after event ingestion."""

    transaction_id: uuid.UUID
    merchant_id: str
    current_status: TransactionStatus
    amount: Decimal
    currency: str
    initiated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EventIngestResponse(BaseModel):
    """Envelope returned by POST /events for both new and duplicate events."""

    success: bool
    message: str
    event: EventData
    transaction: TransactionSnapshot
