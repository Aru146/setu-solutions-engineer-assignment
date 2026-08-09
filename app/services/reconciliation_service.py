"""Reconciliation service.

Owns query logic for GET /reconciliation/summary:
- Aggregation grouped by one of three dimensions: merchant, date, status
- Single SQL query per request with conditional SUM(CASE WHEN ...) per status
- JOIN to merchants for merchant_name (merchant grouping only)
- No ORM objects loaded, no N+1
"""

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import case, func, select, cast
from sqlalchemy.orm import Session
from sqlalchemy.types import Date

from app.logging_config import get_logger
from app.models.enums import SummaryGroupBy, TransactionStatus
from app.models.merchant import Merchant
from app.models.transaction import Transaction

logger = get_logger(__name__)


# ── Return types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MerchantSummaryRow:
    """Backward-compat per-merchant row (returned when group_by == merchant)."""

    merchant_id: str
    merchant_name: str
    total_transactions: int
    payment_initiated_count: int
    payment_processed_count: int
    settled_count: int
    failed_count: int


@dataclass(frozen=True)
class SummaryGroupRow:
    """Generic group row: one per group-key in the aggregation.

    - ``group_key`` is the merchant_id / ISO date string / status name depending
      on which dimension was grouped on.
    - ``merchant_name`` is only populated when grouping by merchant.
    """

    group_key: str
    total_transactions: int
    payment_initiated_count: int
    payment_processed_count: int
    settled_count: int
    failed_count: int
    merchant_name: Optional[str] = None


@dataclass(frozen=True)
class SummaryResult:
    """Return type for get_reconciliation_summary."""

    group_by: SummaryGroupBy
    groups: list[SummaryGroupRow]
    # Backward-compat: only populated when group_by == merchant.
    merchants: list[MerchantSummaryRow] = field(default_factory=list)


# ── Conditional aggregation helpers ──────────────────────────────────────────


def _status_count(status_value: str, label: str):
    """Return a SUM(CASE WHEN current_status = <value> THEN 1 ELSE 0 END) column.

    ELSE 0 guarantees SUM never returns NULL for a group, so downstream code
    doesn't need coalesce().
    """
    return func.sum(
        case((Transaction.current_status == status_value, 1), else_=0)
    ).label(label)


def _select_status_counts():
    """Standard bundle of aggregation columns used across all group_by modes."""
    return (
        func.count(Transaction.transaction_id).label("total_transactions"),
        _status_count(TransactionStatus.PAYMENT_INITIATED.value, "payment_initiated_count"),
        _status_count(TransactionStatus.PAYMENT_PROCESSED.value, "payment_processed_count"),
        _status_count(TransactionStatus.SETTLED.value, "settled_count"),
        _status_count(TransactionStatus.PAYMENT_FAILED.value, "failed_count"),
    )


# ── Group-specific queries ───────────────────────────────────────────────────


def _summary_by_merchant(db: Session) -> tuple[list[SummaryGroupRow], list[MerchantSummaryRow]]:
    """Aggregate transactions grouped by merchant_id.

    Uses INNER JOIN to merchants to pull the human-readable name into the
    result. Composite index ``ix_txn_merchant_status`` covers the GROUP BY
    on merchant_id and the CASE-expression reads of current_status.
    """
    stmt = (
        select(
            Transaction.merchant_id,
            Merchant.merchant_name,
            *_select_status_counts(),
        )
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
        .group_by(Transaction.merchant_id, Merchant.merchant_name)
        .order_by(Merchant.merchant_name)
    )
    rows = db.execute(stmt).all()

    groups = [
        SummaryGroupRow(
            group_key=r.merchant_id,
            merchant_name=r.merchant_name,
            total_transactions=r.total_transactions,
            payment_initiated_count=int(r.payment_initiated_count),
            payment_processed_count=int(r.payment_processed_count),
            settled_count=int(r.settled_count),
            failed_count=int(r.failed_count),
        )
        for r in rows
    ]
    merchants = [
        MerchantSummaryRow(
            merchant_id=r.merchant_id,
            merchant_name=r.merchant_name,
            total_transactions=r.total_transactions,
            payment_initiated_count=int(r.payment_initiated_count),
            payment_processed_count=int(r.payment_processed_count),
            settled_count=int(r.settled_count),
            failed_count=int(r.failed_count),
        )
        for r in rows
    ]
    return groups, merchants


def _summary_by_date(db: Session) -> list[SummaryGroupRow]:
    """Aggregate transactions grouped by the UTC date of ``initiated_at``.

    ``CAST(initiated_at AS DATE)`` in PostgreSQL truncates the timestamptz
    to a calendar date in the server's timezone. All timestamps are stored
    as UTC (``TIMESTAMPTZ``), so the resulting date is the UTC date of the
    business timestamp.

    Ordered chronologically ascending so operations teams see the earliest
    day first — matches most dashboard-style date summaries.
    """
    date_col = cast(Transaction.initiated_at, Date).label("day")
    stmt = (
        select(date_col, *_select_status_counts())
        .group_by(date_col)
        .order_by(date_col.asc())
    )
    rows = db.execute(stmt).all()
    return [
        SummaryGroupRow(
            group_key=r.day.isoformat(),
            total_transactions=r.total_transactions,
            payment_initiated_count=int(r.payment_initiated_count),
            payment_processed_count=int(r.payment_processed_count),
            settled_count=int(r.settled_count),
            failed_count=int(r.failed_count),
        )
        for r in rows
    ]


def _summary_by_status(db: Session) -> list[SummaryGroupRow]:
    """Aggregate transactions grouped by ``current_status``.

    The per-status counts are still included for consistency with the
    other group-by modes — ``settled_count`` on a ``settled`` group row
    equals ``total_transactions`` on that row, but the shape is uniform
    across all group_by responses which simplifies client rendering.
    """
    stmt = (
        select(Transaction.current_status.label("status"), *_select_status_counts())
        .group_by(Transaction.current_status)
        .order_by(Transaction.current_status.asc())
    )
    rows = db.execute(stmt).all()
    return [
        SummaryGroupRow(
            group_key=r.status,
            total_transactions=r.total_transactions,
            payment_initiated_count=int(r.payment_initiated_count),
            payment_processed_count=int(r.payment_processed_count),
            settled_count=int(r.settled_count),
            failed_count=int(r.failed_count),
        )
        for r in rows
    ]


# ── Public service function ──────────────────────────────────────────────────


def get_reconciliation_summary(
    db: Session,
    group_by: SummaryGroupBy = SummaryGroupBy.MERCHANT,
) -> SummaryResult:
    """Aggregate transaction counts grouped by the requested dimension.

    Group-by modes
    --------------
    - ``merchant`` (default): one row per merchant, with merchant_name.
      Backward-compatible with the original response — the ``merchants`` +
      ``total_merchants`` fields on the response are populated.
    - ``date``: one row per UTC calendar day of ``initiated_at``. Useful for
      daily reconciliation reports.
    - ``status``: one row per lifecycle state. Useful for operational
      dashboards showing "how many transactions are stuck at each stage".

    Args:
        db: SQLAlchemy session.
        group_by: Dimension to aggregate on. Defaults to ``merchant``.

    Returns:
        ``SummaryResult`` with a uniform ``groups`` list plus (for merchant
        grouping) the backward-compatible ``merchants`` list.
    """
    logger.info("Building reconciliation summary", extra={"group_by": group_by.value})

    if group_by == SummaryGroupBy.MERCHANT:
        groups, merchants = _summary_by_merchant(db)
        logger.info("Reconciliation summary built", extra={"group_count": len(groups)})
        return SummaryResult(group_by=group_by, groups=groups, merchants=merchants)

    if group_by == SummaryGroupBy.DATE:
        groups = _summary_by_date(db)
    elif group_by == SummaryGroupBy.STATUS:
        groups = _summary_by_status(db)
    else:  # pragma: no cover — Enum coercion means this branch is unreachable
        raise ValueError(f"Unsupported group_by: {group_by!r}")

    logger.info("Reconciliation summary built", extra={"group_count": len(groups)})
    return SummaryResult(group_by=group_by, groups=groups)
