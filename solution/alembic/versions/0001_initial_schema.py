"""Create initial schema: merchants, transactions, events.

Revision ID: 0001
Revises: (none — this is the root migration)
Create Date: 2026-08-06

Schema summary
--------------
Three tables implement an event-sourced payment reconciliation system:

  merchants
    External merchant registry.  merchant_id is the external string key
    (e.g. "merchant_1") used as the natural primary key.

  transactions
    One row per payment attempt.  current_status is a materialised column
    (updated on each event ingest) enabling efficient status-based filtering
    without per-row correlated subqueries.

  events
    Append-only event log — the source of truth.  event_id is the PRIMARY
    KEY and acts as the idempotency key: duplicate POSTs with the same
    event_id fail with IntegrityError, which the service layer silently
    absorbs.  merchant_id is deliberately absent — it is reachable via the
    FK chain events → transactions → merchants.

Indexes
-------
  ix_txn_merchant_status  (merchant_id, current_status)
      Covers the most common listing filter:
        GET /transactions?merchant_id=X&status=Y
      As a composite index with merchant_id as the leading column, PostgreSQL
      can also satisfy merchant_id-only filters via an index-only scan.

  ix_txn_initiated  (initiated_at)
      Covers date-range filtering and chronological sorting:
        GET /transactions?start_date=...&end_date=...

  ix_evt_transaction  (transaction_id, event_timestamp)
      Dual purpose:
        1. Efficient ordered event history retrieval per transaction:
             GET /transactions/{id}  (fetches all events, ordered by time)
        2. Satisfies the FK index requirement — PostgreSQL does NOT
           automatically create indexes for FK columns (unlike MySQL).
           Without this index, ON DELETE CASCADE and FK lookups would
           require a sequential scan of the events table.

CHECK constraints
-----------------
  ck_transactions_current_status   valid status values only
  ck_transactions_amount_positive  amount > 0
  ck_events_event_type             valid event type values only
  ck_events_amount_positive        amount > 0

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── merchants ─────────────────────────────────────────────────────────────
    op.create_table(
        "merchants",
        sa.Column("merchant_id", sa.String(length=50), nullable=False),
        sa.Column("merchant_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("merchant_id", name="pk_merchants"),
    )

    # ── transactions ──────────────────────────────────────────────────────────
    op.create_table(
        "transactions",
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(length=50), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=False,
        ),
        sa.Column("current_status", sa.String(length=20), nullable=False),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "current_status IN ("
            "'payment_initiated', 'payment_processed', "
            "'payment_failed', 'settled'"
            ")",
            name="ck_transactions_current_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.merchant_id"],
            name="fk_transactions_merchant_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transaction_id", name="pk_transactions"),
    )
    op.create_index(
        "ix_txn_merchant_status",
        "transactions",
        ["merchant_id", "current_status"],
    )
    op.create_index(
        "ix_txn_initiated",
        "transactions",
        ["initiated_at"],
    )

    # ── events ────────────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'INR'"),
            nullable=False,
        ),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'payment_initiated', 'payment_processed', "
            "'payment_failed', 'settled'"
            ")",
            name="ck_events_event_type",
        ),
        sa.CheckConstraint("amount > 0", name="ck_events_amount_positive"),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transaction_id"],
            name="fk_events_transaction_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_events"),
    )
    op.create_index(
        "ix_evt_transaction",
        "events",
        ["transaction_id", "event_timestamp"],
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index("ix_evt_transaction", table_name="events")
    op.drop_table("events")

    op.drop_index("ix_txn_initiated", table_name="transactions")
    op.drop_index("ix_txn_merchant_status", table_name="transactions")
    op.drop_table("transactions")

    op.drop_table("merchants")
