"""Transaction ORM model.

Maps to the ``transactions`` table. A transaction represents a single payment
attempt and is the central entity around which events and reconciliation
reporting are organised.

Design decisions:
- ``current_status`` is a materialised column — updated on every event ingest
  within the same DB transaction.  This keeps the ``GET /transactions`` listing
  fast (simple column filter) without requiring a per-row correlated subquery.
- ``initiated_at`` stores the **business timestamp** from the source event,
  not the system clock.  This is the dimension used for date-range filtering.
- ``created_at`` / ``updated_at`` are system-clock audit columns.
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
from app.models.enums import TransactionStatus


class Transaction(Base):
    """Represents a single payment transaction lifecycle."""

    __tablename__ = "transactions"

    __table_args__ = (
        # Data integrity: only valid status values are permitted.
        CheckConstraint(
            "current_status IN ("
            "'payment_initiated', 'payment_processed', "
            "'payment_failed', 'settled'"
            ")",
            name="ck_transactions_current_status",
        ),
        # Financial integrity: transaction amount must be positive.
        CheckConstraint(
            "amount > 0",
            name="ck_transactions_amount_positive",
        ),
        # ── Indexes ──────────────────────────────────────────────────────────
        # Composite index for the most common listing filter:
        #   GET /transactions?merchant_id=X&status=Y
        # PostgreSQL can also use this index for merchant_id-only filters
        # because merchant_id is the leading column.
        Index(
            "ix_txn_merchant_status",
            "merchant_id",
            "current_status",
        ),
        # Date-range filtering and default chronological sort:
        #   GET /transactions?start_date=...&end_date=...
        Index(
            "ix_txn_initiated",
            "initiated_at",
        ),
    )

    # ── Primary key ──────────────────────────────────────────────────
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        comment="UUID supplied by the external payment system",
    )

    # ── Foreign key ──────────────────────────────────────────────────
    merchant_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("merchants.merchant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="The merchant that owns this transaction",
    )

    # ── Financial fields ─────────────────────────────────────────────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        comment="Transaction amount in the specified currency (never float, always exact decimal)",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        server_default=text("'INR'"),
        comment="ISO 4217 currency code",
    )

    # ── Status (materialised from events) ────────────────────────────
    current_status: Mapped[TransactionStatus] = mapped_column(
        String(20),
        nullable=False,
        comment=(
            "Materialised latest status — set on first event ingest, "
            "updated on every subsequent event within the same DB transaction"
        ),
    )

    # ── Timestamps ───────────────────────────────────────────────────
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment=(
            "Business timestamp from the source payment system "
            "(taken from the payment_initiated event, or the earliest event received)"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="System clock — when this row was first inserted",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="System clock — when this row was last modified",
    )

    # ── Relationships ────────────────────────────────────────────────
    merchant: Mapped["Merchant"] = relationship(  # noqa: F821
        "Merchant",
        back_populates="transactions",
        lazy="select",
    )

    events: Mapped[list["Event"]] = relationship(  # noqa: F821
        "Event",
        back_populates="transaction",
        lazy="select",
        order_by="Event.event_timestamp",
        # Events are the source of truth — deleting a transaction deletes its log.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.transaction_id} "
            f"merchant={self.merchant_id!r} "
            f"status={self.current_status!r}>"
        )
