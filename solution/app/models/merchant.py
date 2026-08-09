"""Merchant ORM model.

Maps to the ``merchants`` table. Merchants are upserted from the
``merchant_id`` / ``merchant_name`` fields embedded in every incoming event.
"""

from datetime import datetime

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from app.database import Base


class Merchant(Base):
    """Represents a merchant that submits payment transactions.

    ``merchant_id`` is an opaque string identifier supplied by the external
    payment system (e.g. ``"merchant_1"``).  It is used as the primary key
    directly — no surrogate integer key is needed because the external ID is
    already stable and compact.
    """

    __tablename__ = "merchants"

    # ── Primary key ──────────────────────────────────────────────────
    merchant_id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
        comment="External merchant identifier supplied by the payment system",
    )

    # ── Attributes ───────────────────────────────────────────────────
    merchant_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable merchant name (e.g. 'FreshBasket')",
    )

    # ── Audit timestamps (server-side, always UTC) ───────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="When this merchant row was first created",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
        comment="When this merchant row was last updated",
    )

    # ── Relationships ────────────────────────────────────────────────
    transactions: Mapped[list["Transaction"]] = relationship(  # noqa: F821
        "Transaction",
        back_populates="merchant",
        lazy="select",
        # Cascade: if a merchant is deleted, delete their transactions too.
        # This is defensively correct; in practice merchants are never deleted.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Merchant id={self.merchant_id!r} name={self.merchant_name!r}>"
