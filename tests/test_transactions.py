"""Tests for GET /transactions and GET /transactions/{transaction_id} endpoints."""

import uuid
import pytest
from fastapi.testclient import TestClient


def test_list_transactions_pagination(client: TestClient, make_event):
    """Test offset/limit pagination on GET /transactions."""
    merchant_id = "merchant_paginated"
    # Create 5 distinct transactions
    for i in range(5):
        txn_id = str(uuid.uuid4())
        payload = make_event(
            transaction_id=txn_id,
            merchant_id=merchant_id,
            timestamp=f"2026-01-10T10:0{i}:00+00:00",
        )
        resp = client.post("/events", json=payload)
        assert resp.status_code == 201

    # Page 1: limit=2, offset=0
    r1 = client.get(f"/transactions?merchant_id={merchant_id}&limit=2&offset=0")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["total"] == 5
    assert len(d1["items"]) == 2
    assert d1["limit"] == 2
    assert d1["offset"] == 0

    # Page 2: limit=2, offset=2
    r2 = client.get(f"/transactions?merchant_id={merchant_id}&limit=2&offset=2")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["total"] == 5
    assert len(d2["items"]) == 2
    assert d2["offset"] == 2

    # Page 3: limit=2, offset=4
    r3 = client.get(f"/transactions?merchant_id={merchant_id}&limit=2&offset=4")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["total"] == 5
    assert len(d3["items"]) == 1
    assert d3["offset"] == 4


def test_list_transactions_merchant_filter(client: TestClient, make_event):
    """Test filtering transactions by merchant_id."""
    m1, m2 = "merchant_filter_1", "merchant_filter_2"

    txn1_id = str(uuid.uuid4())
    txn2_id = str(uuid.uuid4())

    client.post("/events", json=make_event(transaction_id=txn1_id, merchant_id=m1, merchant_name="M1"))
    client.post("/events", json=make_event(transaction_id=txn2_id, merchant_id=m2, merchant_name="M2"))

    # Query for m1
    res1 = client.get(f"/transactions?merchant_id={m1}")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["total"] == 1
    assert data1["items"][0]["merchant_id"] == m1
    assert data1["items"][0]["transaction_id"] == txn1_id

    # Query for m2
    res2 = client.get(f"/transactions?merchant_id={m2}")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["total"] == 1
    assert data2["items"][0]["merchant_id"] == m2


def test_list_transactions_status_filter(client: TestClient, make_event):
    """Test filtering transactions by current_status."""
    m_id = "merchant_status_test"

    txn1 = str(uuid.uuid4())
    txn2 = str(uuid.uuid4())

    # txn1 -> payment_initiated
    client.post("/events", json=make_event(transaction_id=txn1, merchant_id=m_id, event_type="payment_initiated"))

    # txn2 -> settled
    client.post("/events", json=make_event(transaction_id=txn2, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn2, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:01:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn2, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:02:00+00:00"))

    # Filter by status=settled
    resp = client.get(f"/transactions?merchant_id={m_id}&current_status=settled")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["transaction_id"] == txn2
    assert data["items"][0]["current_status"] == "settled"


def test_list_transactions_date_filter(client: TestClient, make_event):
    """Test filtering transactions by start_date and end_date."""
    m_id = "merchant_date_test"

    t1 = str(uuid.uuid4())  # Jan 5
    t2 = str(uuid.uuid4())  # Jan 10
    t3 = str(uuid.uuid4())  # Jan 15

    client.post("/events", json=make_event(transaction_id=t1, merchant_id=m_id, timestamp="2026-01-05T00:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=t2, merchant_id=m_id, timestamp="2026-01-10T00:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=t3, merchant_id=m_id, timestamp="2026-01-15T00:00:00+00:00"))

    # Date range: Jan 8 to Jan 12 -> should return only t2
    resp = client.get(f"/transactions?merchant_id={m_id}&start_date=2026-01-08T00:00:00Z&end_date=2026-01-12T00:00:00Z")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["transaction_id"] == t2


def test_list_transactions_empty_results(client: TestClient):
    """Test GET /transactions when no transactions match the query."""
    resp = client.get("/transactions?merchant_id=non_existent_merchant_xyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_list_transactions_sort_by_amount_desc(client: TestClient, make_event):
    """Test sort_by=amount&order=desc returns transactions from highest to lowest amount."""
    m_id = "merchant_sort_amount"

    # Deliberately ingest amounts out of order so the sort has to do work.
    amounts = ["500.00", "100.00", "900.00", "300.00"]
    for i, amount in enumerate(amounts):
        payload = make_event(
            transaction_id=str(uuid.uuid4()),
            merchant_id=m_id,
            amount=amount,
            timestamp=f"2026-01-10T10:0{i}:00+00:00",
        )
        assert client.post("/events", json=payload).status_code == 201

    resp = client.get(f"/transactions?merchant_id={m_id}&sort_by=amount&order=desc")
    assert resp.status_code == 200
    items = resp.json()["items"]
    returned_amounts = [float(item["amount"]) for item in items]
    assert returned_amounts == sorted(returned_amounts, reverse=True)
    assert returned_amounts[0] == 900.00
    assert returned_amounts[-1] == 100.00


def test_list_transactions_sort_by_amount_asc(client: TestClient, make_event):
    """Test sort_by=amount&order=asc returns transactions from lowest to highest amount."""
    m_id = "merchant_sort_amount_asc"

    for i, amount in enumerate(["500.00", "100.00", "900.00", "300.00"]):
        client.post("/events", json=make_event(
            transaction_id=str(uuid.uuid4()),
            merchant_id=m_id,
            amount=amount,
            timestamp=f"2026-01-10T10:0{i}:00+00:00",
        ))

    resp = client.get(f"/transactions?merchant_id={m_id}&sort_by=amount&order=asc")
    assert resp.status_code == 200
    items = resp.json()["items"]
    returned_amounts = [float(item["amount"]) for item in items]
    assert returned_amounts == sorted(returned_amounts)
    assert returned_amounts[0] == 100.00


def test_list_transactions_invalid_sort_by_rejected(client: TestClient):
    """Unknown sort_by values should be rejected with 422 by the enum whitelist."""
    resp = client.get("/transactions?sort_by=malicious_column")
    assert resp.status_code == 422


def test_list_transactions_invalid_order_rejected(client: TestClient):
    """Unknown order values should be rejected with 422 by the enum whitelist."""
    resp = client.get("/transactions?order=sideways")
    assert resp.status_code == 422


def test_get_transaction_detail_success(client: TestClient, make_event):
    """Test successful GET /transactions/{transaction_id} with ordered event history."""
    txn_id = str(uuid.uuid4())
    m_id = "merchant_detail_test"

    # Ingest 3 events in order
    e1 = make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00")
    e2 = make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:05:00+00:00")
    e3 = make_event(transaction_id=txn_id, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:10:00+00:00")

    client.post("/events", json=e1)
    client.post("/events", json=e2)
    client.post("/events", json=e3)

    resp = client.get(f"/transactions/{txn_id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["transaction_id"] == txn_id
    assert data["merchant_id"] == m_id
    assert data["current_status"] == "settled"
    assert len(data["events"]) == 3
    assert data["events"][0]["event_type"] == "payment_initiated"
    assert data["events"][1]["event_type"] == "payment_processed"
    assert data["events"][2]["event_type"] == "settled"


def test_get_transaction_detail_404(client: TestClient):
    """Test GET /transactions/{transaction_id} returns 404 when transaction does not exist."""
    random_id = str(uuid.uuid4())
    resp = client.get(f"/transactions/{random_id}")
    assert resp.status_code == 404
    data = resp.json()
    assert "not found" in data["detail"].lower()
