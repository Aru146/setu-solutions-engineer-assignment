"""Tests for POST /events endpoint."""

import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.merchant import Merchant
from app.models.transaction import Transaction
from app.models.enums import TransactionStatus


def test_event_successful_ingestion(client: TestClient, make_event, db_session: Session):
    """Test successful ingestion of a valid payment lifecycle event."""
    txn_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    payload = make_event(
        event_id=event_id,
        transaction_id=txn_id,
        event_type="payment_initiated",
        merchant_id="merchant_100",
        merchant_name="Merchant 100",
        amount="250.50",
        currency="INR",
        timestamp="2026-01-10T12:00:00+00:00",
    )

    response = client.post("/events", json=payload)
    assert response.status_code == 201
    data = response.json()

    assert data["success"] is True
    assert data["message"] == "Event ingested successfully"
    assert data["event"]["event_id"] == event_id
    assert data["transaction"]["transaction_id"] == txn_id
    assert data["transaction"]["current_status"] == "payment_initiated"

    # Verify DB persistence
    db_event = db_session.execute(select(Event).where(Event.event_id == uuid.UUID(event_id))).scalar_one_or_none()
    assert db_event is not None
    assert db_event.transaction_id == uuid.UUID(txn_id)

    db_txn = db_session.execute(select(Transaction).where(Transaction.transaction_id == uuid.UUID(txn_id))).scalar_one_or_none()
    assert db_txn is not None
    assert db_txn.current_status == TransactionStatus.PAYMENT_INITIATED


def test_event_duplicate_idempotency(client: TestClient, make_event, db_session: Session):
    """Test duplicate event ingestion returns HTTP 200 and does not duplicate state."""
    payload = make_event()

    # First ingestion -> 201 Created
    resp1 = client.post("/events", json=payload)
    assert resp1.status_code == 201
    data1 = resp1.json()
    assert data1["message"] == "Event ingested successfully"

    # Duplicate ingestion -> 200 OK
    resp2 = client.post("/events", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True
    assert data2["message"] == "Event already ingested"

    # Ensure only 1 event recorded in DB
    events = db_session.execute(select(Event).where(Event.event_id == uuid.UUID(payload["event_id"]))).scalars().all()
    assert len(events) == 1


def test_event_concurrent_duplicate(client: TestClient, make_event, db_session: Session):
    """Test concurrent duplicate submissions of the same event payload."""
    payload = make_event()

    def send_event():
        return client.post("/events", json=payload)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send_event) for _ in range(5)]
        responses = [f.result() for f in futures]

    status_codes = [r.status_code for r in responses]
    assert all(code in (200, 201) for code in status_codes)
    assert 201 in status_codes

    # DB must have exactly 1 event record
    events = db_session.execute(select(Event).where(Event.event_id == uuid.UUID(payload["event_id"]))).scalars().all()
    assert len(events) == 1


def test_event_invalid_payload(client: TestClient):
    """Test invalid payloads return 422 Unprocessable Entity."""
    # Missing required field merchant_id
    invalid_payload_1 = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "amount": "100.00",
        "currency": "INR",
        "timestamp": "2026-01-10T12:00:00+00:00",
    }
    resp1 = client.post("/events", json=invalid_payload_1)
    assert resp1.status_code == 422

    # Invalid event_type
    invalid_payload_2 = {
        "event_id": str(uuid.uuid4()),
        "event_type": "unknown_type",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_1",
        "merchant_name": "Merchant 1",
        "amount": "100.00",
        "currency": "INR",
        "timestamp": "2026-01-10T12:00:00+00:00",
    }
    resp2 = client.post("/events", json=invalid_payload_2)
    assert resp2.status_code == 422


def test_event_out_of_order(client: TestClient, make_event, db_session: Session):
    """Test out-of-order events update current_status based on latest event timestamp."""
    txn_id = str(uuid.uuid4())

    # Ingest payment_processed first (timestamp 12:10:00)
    event_processed = make_event(
        transaction_id=txn_id,
        event_type="payment_processed",
        timestamp="2026-01-10T12:10:00+00:00",
    )
    resp1 = client.post("/events", json=event_processed)
    assert resp1.status_code == 201
    assert resp1.json()["transaction"]["current_status"] == "payment_processed"

    # Ingest payment_initiated later (timestamp 12:00:00 - earlier than processed)
    event_initiated = make_event(
        transaction_id=txn_id,
        event_type="payment_initiated",
        timestamp="2026-01-10T12:00:00+00:00",
    )
    resp2 = client.post("/events", json=event_initiated)
    assert resp2.status_code == 201

    # Current status must remain payment_processed because 12:10:00 is later than 12:00:00
    db_txn = db_session.execute(select(Transaction).where(Transaction.transaction_id == uuid.UUID(txn_id))).scalar_one_or_none()
    assert db_txn is not None
    assert db_txn.current_status == TransactionStatus.PAYMENT_PROCESSED


def test_event_merchant_auto_creation(client: TestClient, make_event, db_session: Session):
    """Test that ingesting an event automatically creates a new merchant record if not found."""
    new_merchant_id = f"auto_merchant_{uuid.uuid4().hex[:8]}"
    payload = make_event(
        merchant_id=new_merchant_id,
        merchant_name="Auto Created Merchant",
    )

    # Ensure merchant doesn't exist prior to request
    existing_merchant = db_session.execute(select(Merchant).where(Merchant.merchant_id == new_merchant_id)).scalar_one_or_none()
    assert existing_merchant is None

    resp = client.post("/events", json=payload)
    assert resp.status_code == 201

    # Verify merchant was auto-created
    created_merchant = db_session.execute(select(Merchant).where(Merchant.merchant_id == new_merchant_id)).scalar_one_or_none()
    assert created_merchant is not None
    assert created_merchant.merchant_name == "Auto Created Merchant"
