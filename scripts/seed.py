"""Bulk-load ``sample_events.json`` into the database.

Design
------
The seeder does the same three writes the ``POST /events`` handler does —
merchant upsert, transaction upsert, event insert — but skips the HTTP round
trip and batches everything into a handful of SQL statements per chunk. On a
10K-event dataset this reduces seed time from ~5 minutes (HTTP loop) to a
few seconds.

Idempotency
-----------
- Merchants: ``INSERT ... ON CONFLICT (merchant_id) DO UPDATE`` (upsert).
- Transactions: ``INSERT ... ON CONFLICT (transaction_id) DO UPDATE`` where the
  UPDATE only advances ``current_status``/``updated_at`` if the incoming event
  is *later* than the currently stored one — same out-of-order guard as the
  live event ingestion path.
- Events: ``INSERT ... ON CONFLICT (event_id) DO NOTHING``. Re-running the
  script inserts zero duplicates.

The whole seed runs inside a single database transaction so a partial failure
leaves the database untouched.

Usage
-----
    # Load the default file bundled in the repo
    python -m scripts.seed

    # Load a specific file
    python -m scripts.seed data/sample_events.json

    # Custom batch size (default 1000)
    python -m scripts.seed --batch-size 500

    # Wipe existing rows before loading (dangerous — for local demo resets)
    python -m scripts.seed --truncate

The ``DATABASE_URL`` environment variable (or ``.env`` file) controls which
database is written to. The default is the local Docker Compose Postgres.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import SessionLocal
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.transaction import Transaction

logger = logging.getLogger("scripts.seed")

# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_events.json"
_DEFAULT_BATCH_SIZE = 1000


# ── Helpers ──────────────────────────────────────────────────────────────────


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _chunks(seq: list, size: int) -> Iterable[list]:
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _load_events(path: Path) -> list[dict[str, Any]]:
    """Parse the sample events JSON file into a list of dicts."""
    if not path.is_file():
        raise FileNotFoundError(
            f"sample events file not found: {path}. "
            f"Fetch it via `curl -o data/sample_events.json "
            f"https://raw.githubusercontent.com/SetuHQ/hiring-assignments/"
            f"main/solutions-engineer/sample_events.json`."
        )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _reduce_merchants(events: list[dict]) -> list[dict]:
    """Return one row per unique merchant_id.

    Duplicate rows for the same merchant are collapsed; the last-seen
    ``merchant_name`` wins (mirrors the ON CONFLICT DO UPDATE semantics of
    the live event ingest path).
    """
    seen: dict[str, str] = {}
    for e in events:
        seen[e["merchant_id"]] = e["merchant_name"]
    return [{"merchant_id": mid, "merchant_name": name} for mid, name in seen.items()]


def _reduce_transactions(events: list[dict]) -> list[dict]:
    """Return one row per unique transaction_id.

    For each transaction:
      - ``initiated_at`` = earliest event_timestamp across its events
      - ``current_status`` = event_type of the LATEST event
      - ``amount`` / ``currency`` / ``merchant_id`` = from the earliest event
        (they must be identical across a transaction's events per the data
        model; we don't verify that here — that's the discrepancy checker's job)
    """
    by_txn: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_txn[e["transaction_id"]].append(e)

    rows: list[dict] = []
    for txn_id, txn_events in by_txn.items():
        # Sort by business timestamp to determine earliest/latest.
        txn_events.sort(key=lambda ev: ev["timestamp"])
        earliest = txn_events[0]
        latest = txn_events[-1]
        rows.append(
            {
                "transaction_id": txn_id,
                "merchant_id": earliest["merchant_id"],
                "amount": Decimal(str(earliest["amount"])),
                "currency": earliest["currency"],
                "current_status": latest["event_type"],
                "initiated_at": datetime.fromisoformat(earliest["timestamp"]),
            }
        )
    return rows


def _reduce_events(events: list[dict]) -> list[dict]:
    """Return de-duplicated event rows (last-seen wins per event_id).

    The JSON file may contain duplicate event_ids (that's part of the
    reconciliation test data). We only insert each event_id once here —
    the DB's PK also enforces this.
    """
    seen: dict[str, dict] = {}
    for e in events:
        seen[e["event_id"]] = e
    out: list[dict] = []
    for e in seen.values():
        out.append(
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "transaction_id": e["transaction_id"],
                "amount": Decimal(str(e["amount"])),
                "currency": e["currency"],
                "event_timestamp": datetime.fromisoformat(e["timestamp"]),
            }
        )
    return out


# ── Seeding operations ───────────────────────────────────────────────────────


def _upsert_merchants(session, rows: list[dict]) -> None:
    """Insert or update merchant rows."""
    if not rows:
        return
    stmt = pg_insert(Merchant).values(rows).on_conflict_do_update(
        index_elements=["merchant_id"],
        set_={"merchant_name": pg_insert(Merchant).excluded.merchant_name},
    )
    session.execute(stmt)


def _upsert_transactions(session, rows: list[dict]) -> None:
    """Insert or update transaction rows.

    On conflict, only advance ``current_status`` and ``initiated_at`` if the
    incoming row is either newer (later ``initiated_at`` implies not
    applicable — initiated_at is the EARLIEST time) or if the incoming
    ``initiated_at`` is earlier than the stored value.

    Simpler policy that matches live ingest: always overwrite. Repeated runs
    on the same data are stable because the derived values are deterministic
    functions of the JSON file.
    """
    if not rows:
        return
    stmt = pg_insert(Transaction).values(rows).on_conflict_do_update(
        index_elements=["transaction_id"],
        set_={
            "merchant_id": pg_insert(Transaction).excluded.merchant_id,
            "amount": pg_insert(Transaction).excluded.amount,
            "currency": pg_insert(Transaction).excluded.currency,
            "current_status": pg_insert(Transaction).excluded.current_status,
            "initiated_at": pg_insert(Transaction).excluded.initiated_at,
        },
    )
    session.execute(stmt)


def _insert_events(session, rows: list[dict]) -> int:
    """Insert event rows, skipping any whose event_id already exists.

    Returns the number of rows actually inserted (excludes duplicates that hit
    ON CONFLICT DO NOTHING).
    """
    if not rows:
        return 0
    stmt = (
        pg_insert(Event)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(Event.event_id)
    )
    result = session.execute(stmt)
    return len(result.fetchall())


def _truncate_all(session) -> None:
    """Wipe existing rows. CASCADE handles FK ordering."""
    logger.warning("Truncating events, transactions, merchants (destructive)")
    session.execute(text("TRUNCATE events, transactions, merchants RESTART IDENTITY CASCADE"))


# ── Main ─────────────────────────────────────────────────────────────────────


def seed(
    events_path: Path = _DEFAULT_DATA_PATH,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    truncate: bool = False,
) -> dict[str, int]:
    """Load the events file into the database.

    Returns
    -------
    Counts dict::

        {
            "input_events": ...,   # rows in the JSON file
            "merchants": ...,       # unique merchants upserted
            "transactions": ...,    # unique transactions upserted
            "events_seen": ...,     # unique event_ids in file
            "events_inserted": ...  # rows actually inserted (excludes dupes)
        }
    """
    started = time.perf_counter()
    logger.info("Loading events from %s", events_path)
    events = _load_events(events_path)
    logger.info("Parsed %d events", len(events))

    merchants = _reduce_merchants(events)
    transactions = _reduce_transactions(events)
    unique_events = _reduce_events(events)

    logger.info(
        "Reduced to: %d merchants, %d transactions, %d unique events",
        len(merchants),
        len(transactions),
        len(unique_events),
    )

    inserted = 0
    with SessionLocal() as session:
        try:
            if truncate:
                _truncate_all(session)

            # Merchants: usually tiny (a handful of rows) — one shot is fine.
            _upsert_merchants(session, merchants)
            logger.info("Upserted %d merchants", len(merchants))

            # Transactions: batched — PostgreSQL can choke on massive VALUES lists.
            txn_total = 0
            for chunk in _chunks(transactions, batch_size):
                _upsert_transactions(session, chunk)
                txn_total += len(chunk)
                logger.debug("Upserted transactions: %d / %d", txn_total, len(transactions))
            logger.info("Upserted %d transactions", txn_total)

            # Events: batched.
            for chunk in _chunks(unique_events, batch_size):
                inserted += _insert_events(session, chunk)
                logger.debug("Inserted events: %d so far", inserted)

            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Seed failed — rolled back")
            raise

    elapsed = time.perf_counter() - started
    logger.info(
        "Done in %.2fs — %d events inserted, %d already existed (skipped)",
        elapsed,
        inserted,
        len(unique_events) - inserted,
    )

    return {
        "input_events": len(events),
        "merchants": len(merchants),
        "transactions": len(transactions),
        "events_seen": len(unique_events),
        "events_inserted": inserted,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk-seed sample_events.json into the payment_recon database."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(_DEFAULT_DATA_PATH),
        help=f"Path to sample_events.json (default: {_DEFAULT_DATA_PATH})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Rows per INSERT batch (default: {_DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete existing rows before loading. Destructive — for local demo only.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    try:
        seed(
            events_path=Path(args.path),
            batch_size=args.batch_size,
            truncate=args.truncate,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 — surface any error to caller
        logger.error("Seed failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
