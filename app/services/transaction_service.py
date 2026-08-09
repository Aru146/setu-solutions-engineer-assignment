"""Transaction listing service.

Owns the query logic for GET /transactions:
- filter by merchant_id, current_status, date range
- JOIN to merchants for merchant_name (single query, no N+1)
- count total matching rows for pagination metadata
- configurable sort (whitelisted column + direction)
- offset/limit pagination
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging_config import get_logger
from app.models.enums import SortOrder, TransactionSortField
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionFilters
from app.utils.pagination import PaginationParams

logger = get_logger(__name__)


# ── Sort column mapping ───────────────────────────────────────────────────────
# Whitelist of user-facing sort field names → SQLAlchemy column objects.
# Using a dict rather than getattr() on the model prevents users from sorting
# on internal columns (e.g. created_at, updated_at row internals) or arbitrary
# attributes, and produces a validation error for anything not in the map.
_SORT_COLUMN_MAP = {
    TransactionSortField.INITIATED_AT: Transaction.initiated_at,
    TransactionSortField.UPDATED_AT: Transaction.updated_at,
    TransactionSortField.AMOUNT: Transaction.amount,
    TransactionSortField.MERCHANT_ID: Transaction.merchant_id,
}


@dataclass(frozen=True)
class TransactionRow:
    """Flat projection of a transaction + its merchant name.

    Using a plain dataclass avoids lazy-loading surprises when Pydantic
    serialises the result outside the session scope.
    """

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    amount: Decimal
    currency: str
    current_status: str
    initiated_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TransactionListResult:
    """Return type for list_transactions."""

    items: list[TransactionRow]
    total: int

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Coerce a timezone-naive datetime to UTC-aware.

    Timezone-aware datetimes are returned unchanged.  ``None`` is passed
    through so callers can use this unconditionally on optional fields.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Service ───────────────────────────────────────────────────────────────────


def list_transactions(
    db: Session,
    filters: TransactionFilters,
    pagination: PaginationParams,
) -> TransactionListResult:
    """Query transactions with filters, joined merchant name, and pagination.

    Query strategy
    --------------
    A single SELECT with an INNER JOIN on merchants avoids N+1 while keeping
    the result flat.  The same WHERE clause is reused for the COUNT query so
    the two queries are always consistent.

    Indexes used
    ------------
    - ix_txn_merchant_status (merchant_id, current_status)
        → used when filtering by merchant_id alone or (merchant_id + status)
    - ix_txn_initiated (initiated_at)
        → used for date-range filters and ORDER BY initiated_at DESC
    - PostgreSQL planner combines both via bitmap index scan when all four
      filters are active.

    Args:
        db:         SQLAlchemy session.
        filters:    Validated filter parameters.
        pagination: Limit/offset from query params.

    Returns:
        ``TransactionListResult`` with flat rows and total count.
    """
    log_extra = {
        "merchant_id": filters.merchant_id,
        "status": filters.current_status,
        "start_date": filters.start_date,
        "end_date": filters.end_date,
        "sort_by": filters.sort_by,
        "order": filters.order,
        "limit": pagination.limit,
        "offset": pagination.offset,
    }
    logger.info("Listing transactions", extra=log_extra)

    # ── Normalise date filters to UTC-aware datetimes ────────────────
    # FastAPI parses ISO 8601 strings without a timezone offset as naive
    # datetimes.  Comparing a naive datetime against a TIMESTAMPTZ column
    # via psycopg2 raises DataError at runtime.  We treat naive input as UTC,
    # which is the only safe assumption and matches the storage convention.
    start_date = _ensure_utc(filters.start_date)
    end_date = _ensure_utc(filters.end_date)

    # ── Build the base WHERE predicate list ──────────────────────────
    conditions = []

    if filters.merchant_id is not None:
        conditions.append(Transaction.merchant_id == filters.merchant_id)

    if filters.current_status is not None:
        conditions.append(Transaction.current_status == filters.current_status.value)

    if start_date is not None:
        conditions.append(Transaction.initiated_at >= start_date)

    if end_date is not None:
        conditions.append(Transaction.initiated_at <= end_date)

    # ── COUNT query (same WHERE, no JOIN needed) ─────────────────────
    # count_stmt stays cheap: it only hits the transactions table.
    count_stmt = select(func.count(Transaction.transaction_id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)

    total: int = db.execute(count_stmt).scalar_one()

    # ── Data query: JOIN merchants for merchant_name in one round-trip ─
    # Selecting individual columns (not ORM objects) returns NamedTuple rows
    # that Pydantic's from_attributes can read directly.
    #
    # Sort: resolve the sort column via the whitelist map, apply direction,
    # and add transaction_id as a tie-breaker so the ordering is total and
    # pagination is deterministic across pages (two rows with the same
    # sort-column value never swap positions between requests).
    sort_column = _SORT_COLUMN_MAP[filters.sort_by]
    direction = sort_column.desc() if filters.order == SortOrder.DESC else sort_column.asc()

    data_stmt = (
        select(
            Transaction.transaction_id,
            Transaction.merchant_id,
            Merchant.merchant_name,          # denormalised join column
            Transaction.amount,
            Transaction.currency,
            Transaction.current_status,
            Transaction.initiated_at,
            Transaction.updated_at,
        )
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
        .order_by(direction, Transaction.transaction_id.asc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    )
    if conditions:
        data_stmt = data_stmt.where(*conditions)

    rows = db.execute(data_stmt).all()

    items = [
        TransactionRow(
            transaction_id=r.transaction_id,
            merchant_id=r.merchant_id,
            merchant_name=r.merchant_name,
            amount=r.amount,
            currency=r.currency,
            current_status=r.current_status,
            initiated_at=r.initiated_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    logger.info(
        "Transaction listing complete",
        extra={**log_extra, "returned": len(items), "total": total},
    )
    return TransactionListResult(items=items, total=total)


# ── Detail query dataclasses ──────────────────────────────────────────────────


@dataclass(frozen=True)
class EventRow:
    """Flat projection of a single event for the detail response."""

    event_id: uuid.UUID
    event_type: str
    amount: Decimal
    currency: str
    event_timestamp: datetime


@dataclass(frozen=True)
class TransactionDetailResult:
    """Return type for get_transaction_by_id."""

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    amount: Decimal
    currency: str
    current_status: str
    initiated_at: datetime
    updated_at: datetime
    events: list[EventRow]


# ── Detail service function ───────────────────────────────────────────────────


def get_transaction_by_id(
    db: Session,
    transaction_id: uuid.UUID,
) -> TransactionDetailResult | None:
    """Fetch a single transaction with its full event history.

    Query strategy
    --------------
    Two queries, both index-targeted — no N+1:

    Query 1 — PK lookup + merchant JOIN (single row):
        SELECT t.*, m.merchant_name
        FROM transactions t
        JOIN merchants m ON t.merchant_id = m.merchant_id
        WHERE t.transaction_id = ?

    Query 2 — all events for the transaction, chronologically:
        SELECT event_id, event_type, amount, currency, event_timestamp
        FROM events
        WHERE transaction_id = ?
        ORDER BY event_timestamp ASC
        → uses ix_evt_transaction (transaction_id, event_timestamp)

    The JOIN in Query 1 avoids a separate merchant SELECT.
    Query 2 fetches all events in one round-trip regardless of count.

    Args:
        db:             SQLAlchemy session.
        transaction_id: UUID to look up.

    Returns:
        ``TransactionDetailResult`` if found, ``None`` if the transaction
        does not exist (caller raises 404).
    """
    logger.info("Fetching transaction detail", extra={"transaction_id": str(transaction_id)})

    # ── Query 1: transaction row + merchant name ──────────────────────
    txn_row = db.execute(
        select(
            Transaction.transaction_id,
            Transaction.merchant_id,
            Merchant.merchant_name,
            Transaction.amount,
            Transaction.currency,
            Transaction.current_status,
            Transaction.initiated_at,
            Transaction.updated_at,
        )
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
        .where(Transaction.transaction_id == transaction_id)
    ).first()

    if txn_row is None:
        logger.info("Transaction not found", extra={"transaction_id": str(transaction_id)})
        return None

    # ── Query 2: all events ordered chronologically ───────────────────
    # Uses ix_evt_transaction (transaction_id, event_timestamp) — covers
    # both the WHERE predicate and the ORDER BY in a single index scan.
    event_rows = db.execute(
        select(
            Event.event_id,
            Event.event_type,
            Event.amount,
            Event.currency,
            Event.event_timestamp,
        )
        .where(Event.transaction_id == transaction_id)
        .order_by(Event.event_timestamp.asc())
    ).all()

    logger.info(
        "Transaction detail fetched",
        extra={"transaction_id": str(transaction_id), "event_count": len(event_rows)},
    )

    return TransactionDetailResult(
        transaction_id=txn_row.transaction_id,
        merchant_id=txn_row.merchant_id,
        merchant_name=txn_row.merchant_name,
        amount=txn_row.amount,
        currency=txn_row.currency,
        current_status=txn_row.current_status,
        initiated_at=txn_row.initiated_at,
        updated_at=txn_row.updated_at,
        events=[
            EventRow(
                event_id=e.event_id,
                event_type=e.event_type,
                amount=e.amount,
                currency=e.currency,
                event_timestamp=e.event_timestamp,
            )
            for e in event_rows
        ],
    )
