"""Discrepancy detection service.

Owns the query and analysis logic for GET /reconciliation/discrepancies.

Query strategy
--------------
Two database round trips — no N+1:

  Query 1: All transactions with merchant name (INNER JOIN).
  Query 2: All events for those transaction IDs via a single IN clause,
           ordered by (transaction_id, event_timestamp ASC).

Events are grouped by transaction_id in Python (O(total_events)).
Discrepancy detection runs entirely in Python on the resulting ordered lists.

Discrepancy types
-----------------
  duplicate_event_type              — same event_type appears more than once
  missing_payment_initiated         — no payment_initiated in the event history
  payment_failed_after_settled      — payment_failed timestamp > settled timestamp
  settled_without_payment_processed — settled reached without payment_processed
  processed_never_settled           — stuck in payment_processed past age threshold
  invalid_state_transition          — consecutive events violate the state machine
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_logger
from app.models.enums import TransactionStatus
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.transaction import Transaction

logger = get_logger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────────────

# Default age threshold in hours after which a payment_processed transaction is
# considered stale (i.e. "processed but never settled"). The router allows this
# to be overridden per request via the ``stale_after_hours`` query parameter.
_DEFAULT_PROCESSED_STALE_HOURS = 24


# ── State machine ─────────────────────────────────────────────────────────────

# Maps each state (None = before first event) to its valid successor states.
_VALID_TRANSITIONS: dict[str | None, set[str]] = {
    None: {TransactionStatus.PAYMENT_INITIATED.value},
    TransactionStatus.PAYMENT_INITIATED.value: {
        TransactionStatus.PAYMENT_PROCESSED.value,
        TransactionStatus.PAYMENT_FAILED.value,
    },
    TransactionStatus.PAYMENT_PROCESSED.value: {
        TransactionStatus.SETTLED.value,
        TransactionStatus.PAYMENT_FAILED.value,
    },
    TransactionStatus.SETTLED.value: set(),        # terminal state
    TransactionStatus.PAYMENT_FAILED.value: set(), # terminal state
}


# ── Internal data structures ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _EventInfo:
    """Flat projection of a single event row."""

    event_id: uuid.UUID
    event_type: str
    amount: Decimal
    currency: str
    event_timestamp: datetime


@dataclass(frozen=True)
class _TxnInfo:
    """Flat projection of a transaction + merchant row."""

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    current_status: str


@dataclass(frozen=True)
class DiscrepancyRecord:
    """A single detected anomaly — one per discrepancy_type per transaction."""

    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    current_status: str
    discrepancy_type: str
    explanation: str
    event_history: list  # list[_EventInfo], not typed to avoid frozen dataclass issue


@dataclass(frozen=True)
class DiscrepancyResult:
    """Return type for get_discrepancies."""

    discrepancies: list  # list[DiscrepancyRecord]


# ── Detection logic ───────────────────────────────────────────────────────────


def _detect(
    txn: _TxnInfo,
    events: list[_EventInfo],
    *,
    now: datetime,
    processed_stale_after_hours: int,
) -> list[DiscrepancyRecord]:
    """Run all discrepancy checks for a single transaction.

    Each check is independent.  A transaction with multiple anomaly types
    produces one DiscrepancyRecord entry per anomaly, allowing callers to
    filter or group by ``discrepancy_type``.

    Checks are run in order of specificity: duplicate → missing start →
    specific anomalies → general transition validity.  Check 6 reports
    only the first invalid transition to avoid cascading noise.

    Args:
        txn:    Transaction + merchant metadata.
        events: Full event history, ordered by event_timestamp ASC.
        now:    Reference "current" time for age-based checks. Injected so
                tests can pin a deterministic value.
        processed_stale_after_hours:
                Age threshold in hours for the processed_never_settled check.

    Returns:
        List of discrepancy records; empty list if the transaction is clean.
    """
    if not events:
        return []

    results: list[DiscrepancyRecord] = []

    event_types: list[str] = [e.event_type for e in events]
    type_counts: Counter = Counter(event_types)
    type_set: set[str] = set(event_types)

    _pi = TransactionStatus.PAYMENT_INITIATED.value
    _pp = TransactionStatus.PAYMENT_PROCESSED.value
    _st = TransactionStatus.SETTLED.value
    _pf = TransactionStatus.PAYMENT_FAILED.value

    def _make(dtype: str, explanation: str) -> DiscrepancyRecord:
        return DiscrepancyRecord(
            transaction_id=txn.transaction_id,
            merchant_id=txn.merchant_id,
            merchant_name=txn.merchant_name,
            current_status=txn.current_status,
            discrepancy_type=dtype,
            explanation=explanation,
            event_history=list(events),
        )

    # ── Check 1: Duplicate event types ──────────────────────────────
    # Any event_type that appears more than once is an anomaly because
    # each lifecycle step should occur at most once per transaction.
    for etype in sorted(type_counts):          # sorted for deterministic output
        count = type_counts[etype]
        if count > 1:
            results.append(_make(
                "duplicate_event_type",
                f"Event type '{etype}' appears {count} times "
                f"(expected at most once per payment lifecycle).",
            ))

    # ── Check 2: Missing payment_initiated ──────────────────────────
    # Every valid lifecycle begins with payment_initiated.  Its absence
    # means either the event was lost or history is incomplete.
    if _pi not in type_set:
        results.append(_make(
            "missing_payment_initiated",
            "Transaction has no payment_initiated event. "
            "Every payment lifecycle must begin with payment_initiated.",
        ))

    # ── Check 3: payment_failed after settled ───────────────────────
    # Both statuses are terminal.  Receiving payment_failed after a
    # transaction has already settled is a hard violation.
    if _st in type_set and _pf in type_set:
        last_settled_ts = max(e.event_timestamp for e in events if e.event_type == _st)
        earliest_failed_ts = min(e.event_timestamp for e in events if e.event_type == _pf)
        if earliest_failed_ts > last_settled_ts:
            results.append(_make(
                "payment_failed_after_settled",
                "A payment_failed event was received after the transaction "
                "had already reached the terminal 'settled' state.",
            ))

    # ── Check 4: Settled without payment_processed occurring before it ──
    # The expected happy path is: initiated → processed → settled.
    # Checking set membership alone is insufficient: a payment_processed event
    # that arrives only AFTER settled is semantically absent at settlement time.
    # We therefore verify that at least one payment_processed event has a
    # business timestamp strictly earlier than the first settled event.
    if _st in type_set:
        first_settled_ts = min(e.event_timestamp for e in events if e.event_type == _st)
        processed_before_settled = any(
            e.event_type == _pp and e.event_timestamp < first_settled_ts
            for e in events
        )
        if not processed_before_settled:
            results.append(_make(
                "settled_without_payment_processed",
                "Transaction reached 'settled' without a 'payment_processed' "
                "event occurring before it — an expected intermediate step.",
            ))

    # ── Check 5: Processed but never settled (aging) ────────────────
    # The assignment lists "payment marked processed but never settled" as a
    # canonical discrepancy example. A processed transaction is only stuck if
    # enough time has passed without a subsequent settled event — otherwise
    # it's a normal in-flight transaction. We flag processed-only transactions
    # whose latest event is older than the configured threshold.
    #
    # Guard: skip this check when current_status is not payment_processed
    # (once the txn has moved on to settled or failed, it's no longer stuck).
    if txn.current_status == _pp and _st not in type_set:
        latest_event_ts = max(e.event_timestamp for e in events)
        # Make both sides timezone-aware for the comparison.
        latest_utc = (
            latest_event_ts
            if latest_event_ts.tzinfo is not None
            else latest_event_ts.replace(tzinfo=timezone.utc)
        )
        age_hours = (now - latest_utc).total_seconds() / 3600.0
        if age_hours >= processed_stale_after_hours:
            results.append(_make(
                "processed_never_settled",
                f"Transaction has been in 'payment_processed' state for "
                f"{age_hours:.1f} hours with no subsequent settlement "
                f"(threshold: {processed_stale_after_hours}h).",
            ))

    # ── Check 6: Invalid state machine transitions ───────────────────
    # Walk the ordered event list; verify each transition against the
    # valid state machine.  Only the first violation is reported to
    # avoid cascading noise from a single root-cause anomaly.
    prev: str | None = None
    for evt in events:
        allowed = _VALID_TRANSITIONS.get(prev, set())
        if evt.event_type not in allowed:
            results.append(_make(
                "invalid_state_transition",
                f"Transition from '{prev or 'start'}' to '{evt.event_type}' "
                f"is not a valid payment lifecycle step "
                f"(allowed from '{prev or 'start'}': "
                f"{sorted(allowed) or ['none — terminal state']}).",
            ))
            break
        prev = evt.event_type

    return results


# ── Service function ──────────────────────────────────────────────────────────


def get_discrepancies(
    db: Session,
    *,
    processed_stale_after_hours: int = _DEFAULT_PROCESSED_STALE_HOURS,
    now: datetime | None = None,
) -> DiscrepancyResult:
    """Fetch all transactions and detect reconciliation anomalies.

    Args:
        db: SQLAlchemy session.
        processed_stale_after_hours: Age threshold in hours after which a
            transaction stuck in ``payment_processed`` is flagged as
            ``processed_never_settled``. Set to 0 to flag ALL processed-only
            transactions regardless of age.
        now: Reference "current" UTC time for age-based checks. Defaults to
            ``datetime.now(timezone.utc)``. Explicit parameter so tests can
            pin a deterministic value.

    Returns:
        ``DiscrepancyResult`` with all detected discrepancy records.
    """
    reference_now = now if now is not None else datetime.now(timezone.utc)
    logger.info(
        "Starting discrepancy detection",
        extra={
            "processed_stale_after_hours": processed_stale_after_hours,
            "reference_now": reference_now.isoformat(),
        },
    )

    # ── Query 1: all transactions + merchant names ────────────────────
    # No filtering — the reconciliation check covers the full dataset.
    txn_rows = db.execute(
        select(
            Transaction.transaction_id,
            Transaction.merchant_id,
            Merchant.merchant_name,
            Transaction.current_status,
        )
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
    ).all()

    if not txn_rows:
        logger.info("No transactions found — discrepancy check skipped")
        return DiscrepancyResult(discrepancies=[])

    transaction_ids = [r.transaction_id for r in txn_rows]
    logger.info("Fetched transactions for discrepancy check", extra={"count": len(transaction_ids)})

    # ── Query 2: all events in one round-trip ────────────────────────
    # ORDER BY transaction_id, event_timestamp ASC means events arrive
    # pre-sorted for each transaction — no Python sort required after
    # grouping.  The composite index ix_evt_transaction
    # (transaction_id, event_timestamp) satisfies both the IN predicate
    # and the ORDER BY in a single index scan.
    event_rows = db.execute(
        select(
            Event.transaction_id,
            Event.event_id,
            Event.event_type,
            Event.amount,
            Event.currency,
            Event.event_timestamp,
        )
        .where(Event.transaction_id.in_(transaction_ids))
        .order_by(
            Event.transaction_id,
            Event.event_timestamp.asc(),
            # Secondary sort on the PK guarantees a total ordering when two
            # events share the same business timestamp.  event_id is UUID and
            # therefore unique — no two rows can produce a tie on this column.
            # This eliminates the non-deterministic ordering that would otherwise
            # produce false-positive invalid_state_transition discrepancies.
            Event.event_id.asc(),
        )
    ).all()

    logger.info("Fetched events for discrepancy check", extra={"count": len(event_rows)})

    # ── Group events by transaction_id ────────────────────────────────
    # Insertion order from the sorted DB result is preserved by defaultdict.
    events_by_txn: dict[uuid.UUID, list[_EventInfo]] = defaultdict(list)
    for r in event_rows:
        events_by_txn[r.transaction_id].append(
            _EventInfo(
                event_id=r.event_id,
                event_type=r.event_type,
                amount=r.amount,
                currency=r.currency,
                event_timestamp=r.event_timestamp,
            )
        )

    # ── Run detection per transaction ─────────────────────────────────
    all_discrepancies: list[DiscrepancyRecord] = []
    clean_count = 0

    for txn_row in txn_rows:
        txn = _TxnInfo(
            transaction_id=txn_row.transaction_id,
            merchant_id=txn_row.merchant_id,
            merchant_name=txn_row.merchant_name,
            current_status=txn_row.current_status,
        )
        events = events_by_txn.get(txn.transaction_id, [])
        found = _detect(
            txn,
            events,
            now=reference_now,
            processed_stale_after_hours=processed_stale_after_hours,
        )

        if found:
            all_discrepancies.extend(found)
        else:
            clean_count += 1

    logger.info(
        "Discrepancy detection complete",
        extra={
            "total_transactions": len(txn_rows),
            "clean_transactions": clean_count,
            "total_discrepancy_entries": len(all_discrepancies),
        },
    )
    return DiscrepancyResult(discrepancies=all_discrepancies)
