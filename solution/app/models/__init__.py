"""ORM models package.

Importing this package (or any sub-module) registers the models with
``Base.metadata``, which is required for:
- Alembic autogenerate to detect the full schema
- SQLAlchemy ``Base.metadata.create_all()`` in tests

Import order matters for FK resolution:
  1. enums        (no DB deps)
  2. Merchant     (no FK deps)
  3. Transaction  (FK → Merchant)
  4. Event        (FK → Transaction)
"""

from app.models.enums import EventType, TransactionStatus  # noqa: F401
from app.models.merchant import Merchant  # noqa: F401
from app.models.transaction import Transaction  # noqa: F401
from app.models.event import Event  # noqa: F401

__all__ = [
    "EventType",
    "TransactionStatus",
    "Merchant",
    "Transaction",
    "Event",
]
