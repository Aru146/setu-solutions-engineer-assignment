"""Event ingestion service.

Owns all business logic for POST /events:
- merchant upsert (safe under concurrent requests via ON CONFLICT)
- transaction create-or-update
- event insert (idempotent)
- status materialisation (guarded against out-of-order delivery)

All writes execute in a single database transaction.  The service layer
deliberately contains no HTTP concerns — it does not import FastAPI.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging_config import get_logger
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.transaction import Transaction
from app.schemas.event import EventIngestRequest

logger = get_logger(__name__)

# PostgreSQL SQLSTATE code for unique_violation (ISO/IEC 9075).
# Using the code rather than string-matching the human-readable message is
# locale-independent and stable across psycopg2 / psycopg3 versions.
_UNIQUE_VIOLATION = "23505"


# ── Return type ───────────────────────────────────────────────────────────────


class IngestResult:
    """Outcome of a single event ingest call."""

    __slots__ = ("event", "transaction", "is_duplicate")

    def __init__(self, event: Event, transaction: Transaction, *, is_duplicate: bool) -> None:
        self.event = event
        self.transaction = transaction
        self.is_duplicate = is_duplicate


# ── Helpers ───────────────────────────────────────────────────────────────────


def _pgcode(exc: IntegrityError) -> str | None:
    """Return the PostgreSQL SQLSTATE code from a psycopg2 IntegrityError.

    Returns ``None`` when running against a driver that does not expose
    ``exc.orig.pgcode`` (e.g. aiosqlite in unit tests).
    """
    return getattr(exc.orig, "pgcode", None)


def _constraint_name(exc: IntegrityError) -> str | None:
    """Return the violated constraint name via psycopg2 server diagnostics.

    Uses ``exc.orig.diag.constraint_name`` which PostgreSQL populates with the
    exact constraint name defined in DDL.  More reliable than string-searching
    the error message, which varies across server locales and psycopg versions.

    Returns ``None`` when the driver does not expose diagnostics.
    """
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    """True if *exc* is a unique-constraint violation on *constraint*."""
    return _pgcode(exc) == _UNIQUE_VIOLATION and _constraint_name(exc) == constraint


# ── Service ───────────────────────────────────────────────────────────────────


def ingest_event(db: Session, payload: EventIngestRequest) -> IngestResult:
    """Ingest a single payment event.

    Guarantees:
    - **Idempotent**: same ``event_id`` submitted twice → 200, no state change.
    - **Atomic**: merchant upsert + transaction upsert + event insert all commit
      together or not at all.
    - **Consistent status**: ``current_status`` always reflects the LATEST
      event by business timestamp, even when events arrive out-of-order.
    - **Concurrent merchant creation safe**: uses ``INSERT … ON CONFLICT`` so
      two simultaneous requests for a brand-new merchant never race to a
      constraint violation.

    Args:
        db:      SQLAlchemy ``Session`` (injected by FastAPI dependency).
        payload: Validated request body.

    Returns:
        ``IngestResult`` with the persisted event, the transaction snapshot,
        and a flag indicating whether this was a duplicate.
    """
    log_ctx = {
        "event_id": str(payload.event_id),
        "event_type": payload.event_type,
        "transaction_id": str(payload.transaction_id),
        "merchant_id": payload.merchant_id,
    }

    # ── 1. Idempotency pre-check ─────────────────────────────────────
    # SELECT before INSERT handles the common case without locking.
    # The IntegrityError catch on pk_events covers the concurrent-duplicate
    # race condition (two requests with the same event_id in flight together).
    existing_event = db.get(Event, payload.event_id)
    if existing_event is not None:
        logger.info("Duplicate event received — returning cached response", extra=log_ctx)
        existing_txn = db.get(Transaction, existing_event.transaction_id)
        return IngestResult(existing_event, existing_txn, is_duplicate=True)

    logger.info("Ingesting new event", extra=log_ctx)

    try:
        # ── 2. Merchant upsert (Fix 2: safe under concurrent requests) ──
        # INSERT … ON CONFLICT DO UPDATE is atomic at the database level.
        # Two concurrent requests for the same new merchant serialize cleanly:
        # one INSERTs, the other hits the conflict branch and UPDATEs the name.
        # No application-level race handling required.
        db.execute(
            pg_insert(Merchant)
            .values(
                merchant_id=payload.merchant_id,
                merchant_name=payload.merchant_name,
            )
            .on_conflict_do_update(
                index_elements=["merchant_id"],
                set_={"merchant_name": payload.merchant_name},
            )
        )
        db.flush()
        logger.debug("Merchant upserted", extra={"merchant_id": payload.merchant_id})

        # ── 3. Transaction create-or-update ─────────────────────────
        transaction = db.get(Transaction, payload.transaction_id)

        if transaction is None:
            # First event for this transaction — create row.
            transaction = Transaction(
                transaction_id=payload.transaction_id,
                merchant_id=payload.merchant_id,
                amount=payload.amount,
                currency=payload.currency,
                current_status=payload.event_type.value,
                initiated_at=payload.timestamp,
            )
            db.add(transaction)
            db.flush()
            logger.debug(
                "Created new transaction",
                extra={"transaction_id": str(payload.transaction_id), "status": payload.event_type.value},
            )

        else:
            # ── Fix 1: Guard current_status against out-of-order delivery ──
            # Find the business timestamp of the most recent event already
            # stored for this transaction.  We must query the events table
            # directly rather than trusting transaction.updated_at, because
            # updated_at tracks system time (when *we* received the event),
            # not the event's own business timestamp.
            max_existing_ts = db.execute(
                select(func.max(Event.event_timestamp)).where(
                    Event.transaction_id == payload.transaction_id
                )
            ).scalar()

            if max_existing_ts is None or payload.timestamp > max_existing_ts:
                # Incoming event is the new latest — advance materialised status.
                transaction.current_status = payload.event_type.value
                transaction.updated_at = datetime.now(timezone.utc)
                logger.debug(
                    "Transaction status advanced",
                    extra={
                        "transaction_id": str(payload.transaction_id),
                        "new_status": payload.event_type.value,
                    },
                )
            else:
                # Out-of-order: this event is older than the latest known event.
                # Still insert it into the append-only log (history is complete),
                # but do NOT regress current_status to an earlier state.
                logger.info(
                    "Out-of-order event — persisting history, status unchanged",
                    extra={
                        **log_ctx,
                        "event_ts": payload.timestamp.isoformat(),
                        "latest_known_ts": max_existing_ts.isoformat(),
                        "current_status": transaction.current_status,
                    },
                )

            # Always pull initiated_at back if this event is the earliest seen.
            if payload.timestamp < transaction.initiated_at:
                transaction.initiated_at = payload.timestamp

        # ── 4. Insert event (append-only log) ────────────────────────
        event = Event(
            event_id=payload.event_id,
            event_type=payload.event_type.value,
            transaction_id=payload.transaction_id,
            amount=payload.amount,
            currency=payload.currency,
            event_timestamp=payload.timestamp,
        )
        db.add(event)

        db.commit()
        db.refresh(event)
        db.refresh(transaction)

        logger.info("Event ingested successfully", extra=log_ctx)
        return IngestResult(event, transaction, is_duplicate=False)

    except IntegrityError as exc:
        db.rollback()

        # ── Fix 4: Structured IntegrityError classification ──────────
        # Use SQLSTATE (23505 = unique_violation) and the exact constraint
        # name from psycopg2 server diagnostics.  This is locale-independent
        # and does not break if PostgreSQL changes its English error phrasing.
        if _is_unique_violation(exc, "pk_events") or _is_unique_violation(exc, "events_pkey"):
            # Concurrent duplicate: two requests with the same event_id raced.
            # The first writer committed; the second hit the PK constraint.
            # Re-fetch after rollback and return the idempotent response.
            logger.warning(
                "Concurrent duplicate event detected — returning idempotent response",
                extra=log_ctx,
            )
            existing_event = db.get(Event, payload.event_id)
            existing_txn = db.get(Transaction, payload.transaction_id)
            return IngestResult(existing_event, existing_txn, is_duplicate=True)

        # Any other IntegrityError (CHECK constraint, FK violation) is a
        # genuine data problem — log the structured details and re-raise.
        logger.error(
            "IntegrityError during event ingest — not a handled race condition",
            extra={
                **log_ctx,
                "pgcode": _pgcode(exc),
                "constraint": _constraint_name(exc),
                "error": str(exc),
            },
        )
        raise
