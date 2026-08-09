"""Shared enumerations for event types and transaction statuses.

These are the authoritative definitions used by:
- SQLAlchemy models (CHECK constraints reference these values)
- Pydantic schemas (request validation)
- Business logic (state machine transitions)

Defined as Python ``enum.Enum`` rather than SQLAlchemy ``Enum`` so they
remain framework-agnostic and can be imported anywhere without triggering
a database dependency.
"""

import enum


class EventType(str, enum.Enum):
    """All valid payment lifecycle event types.

    Inheriting from ``str`` allows direct comparison with plain strings
    and seamless JSON serialization by Pydantic.
    """

    PAYMENT_INITIATED = "payment_initiated"
    PAYMENT_PROCESSED = "payment_processed"
    PAYMENT_FAILED = "payment_failed"
    SETTLED = "settled"


class TransactionStatus(str, enum.Enum):
    """Current status of a transaction — derived from its latest event.

    The valid lifecycle is::

        payment_initiated → payment_processed → settled   (happy path)
        payment_initiated → payment_failed                (failure path)

    Anything outside these transitions constitutes a discrepancy.
    """

    PAYMENT_INITIATED = "payment_initiated"
    PAYMENT_PROCESSED = "payment_processed"
    PAYMENT_FAILED = "payment_failed"
    SETTLED = "settled"


class TransactionSortField(str, enum.Enum):
    """Allowed sort fields for ``GET /transactions``.

    The whitelist prevents SQL injection through the ``sort_by`` query
    parameter — callers can only sort by columns we've explicitly indexed
    or that are cheap to sort in-memory for a single page.
    """

    INITIATED_AT = "initiated_at"
    UPDATED_AT = "updated_at"
    AMOUNT = "amount"
    MERCHANT_ID = "merchant_id"


class SortOrder(str, enum.Enum):
    """Sort direction for list endpoints."""

    ASC = "asc"
    DESC = "desc"


class SummaryGroupBy(str, enum.Enum):
    """Grouping dimension for ``GET /reconciliation/summary``.

    - ``merchant`` groups by merchant_id (default — matches the original
      response shape).
    - ``date`` groups by the UTC date portion of ``initiated_at`` (one row
      per calendar day).
    - ``status`` groups by ``current_status`` (one row per lifecycle state).
    """

    MERCHANT = "merchant"
    DATE = "date"
    STATUS = "status"
