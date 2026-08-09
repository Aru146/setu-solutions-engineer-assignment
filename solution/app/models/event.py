"""Event ORM model.

Maps to the ``events`` table — the append-only event log that is the
**source of truth** for all transaction state.

Design decisions:
- ``event_id`` (UUID) is the PRIMARY KEY and therefore carries an implicit
  UNIQUE constraint.  This is the idempotency key: attempting to insert a
  duplicate ``event_id`` raises an ``IntegrityError`` which the service layer
  catches and silently accepts (returning 200 OK).
- ``merchant_id`` is intentionally NOT stored here.  The merchant is reachable
  via ``events → transactions → merchants``.  Storing it would be denormalised
  redundancy — the sample data confirms it is always consistent.
- ``amount`` and ``currency`` ARE stored here to preserve full event payload
  fidelity (event sourcing principle: the log is a faithful record of what was
  received from the external system).
- ``event_timestamp`` uses the business clock from the source system.
- ``ingested_at`` uses the server clock — records when *we* received the event.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from app.database import Base
from app.models.enums import EventType


class Event(Base):
    """An individual payment lifecycle event — immutable once written."""

    __tablename__ = "events"

    __table_args__ = (
        # Data integrity: only known event types are permitted.
        CheckConstraint(
            "event_type IN ("
            "'payment_initiated', 'payment_processed', "
            "'payment_failed', 'settled'"
            ")",
            name="ck_events_event_type",
        ),
        # Financial integrity: event amount must be positive.
        CheckConstraint(
            "amount > 0",
            name="ck_events_amount_positive",
        ),
        # ── Index ─────────────────────────────────────────────────────────────
        # Covers two access patterns:
        #   1. GET /transactions/{id}  → fetch all events for a transaction,
        #      ordered chronologically (leading column + ORDER BY event_timestamp)
        #   2. PostgreSQL FK constraint on transaction_id — PG does NOT
        #      automatically index FK columns, so this index also prevents
        #      full-table scans on FK lookups and on DELETE of a transaction.
        Index(
            "ix_evt_transaction",
            "transaction_id",
            "event_timestamp",
        ),
    )

    # ── Primary key (also the idempotency key) ───────────────────────
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        comment=(
            "UUID from the external payment system.  The PRIMARY KEY constraint "
            "is the idempotency mechanism: inserting a duplicate event_id raises "
            "an IntegrityError, which the service layer silently ignores."
        ),
    )

    # ── Event descriptor ─────────────────────────────────────────────
    event_type: Mapped[EventType] = mapped_column(
        String(20),
        nullable=False,
        comment="One of: payment_initiated, payment_processed, payment_failed, settled",
    )

    # ── Foreign key ──────────────────────────────────────────────────
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.transaction_id", ondelete="CASCADE"),
        nullable=False,
        comment="The transaction this event belongs to",
    )

    # ── Financial payload (preserved for event sourcing fidelity) ────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        comment="Amount as received from the source system",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        server_default=text("'INR'"),
        comment="ISO 4217 currency code as received from the source system",
    )

    # ── Timestamps ───────────────────────────────────────────────────
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment=(
            "Business timestamp from the source payment system "
            "(the 'timestamp' field in the incoming event payload)"
        ),
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="System clock — when this event was received and persisted",
    )

    # ── Relationships ────────────────────────────────────────────────
    transaction: Mapped["Transaction"] = relationship(  # noqa: F821
        "Transaction",
        back_populates="events",
        lazy="select",
    )

    # ── Property for schema serialization ────────────────────────────
    @property
    def merchant_id(self) -> str:
        """Expose merchant_id via transaction relationship for Pydantic serialization."""
        return self.transaction.merchant_id if self.transaction else ""

    def __repr__(self) -> str:
        return (
            f"<Event id={self.event_id} "
            f"type={self.event_type!r} "
            f"txn={self.transaction_id}>"
        )

