"""Tests for GET /reconciliation/summary and GET /reconciliation/discrepancies endpoints."""

import uuid
import pytest
from fastapi.testclient import TestClient


def test_reconciliation_summary_aggregation_correctness(client: TestClient, make_event):
    """Test aggregation correctness in GET /reconciliation/summary."""
    mA, mB = "summary_merchant_A", "summary_merchant_B"

    # Merchant A: 1 initiated, 1 settled
    tA1, tA2 = str(uuid.uuid4()), str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=tA1, merchant_id=mA, merchant_name="Merchant A", event_type="payment_initiated"))

    client.post("/events", json=make_event(transaction_id=tA2, merchant_id=mA, merchant_name="Merchant A", event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=tA2, merchant_id=mA, merchant_name="Merchant A", event_type="payment_processed", timestamp="2026-01-10T10:01:00+00:00"))
    client.post("/events", json=make_event(transaction_id=tA2, merchant_id=mA, merchant_name="Merchant A", event_type="settled", timestamp="2026-01-10T10:02:00+00:00"))

    # Merchant B: 1 payment_failed
    tB1 = str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=tB1, merchant_id=mB, merchant_name="Merchant B", event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=tB1, merchant_id=mB, merchant_name="Merchant B", event_type="payment_failed", timestamp="2026-01-10T10:01:00+00:00"))

    resp = client.get("/reconciliation/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_merchants"] == 2
    merchants = {m["merchant_id"]: m for m in data["merchants"]}

    assert mA in merchants
    mA_summary = merchants[mA]
    assert mA_summary["total_transactions"] == 2
    assert mA_summary["payment_initiated_count"] == 1
    assert mA_summary["settled_count"] == 1
    assert mA_summary["payment_processed_count"] == 0
    assert mA_summary["failed_count"] == 0

    assert mB in merchants
    mB_summary = merchants[mB]
    assert mB_summary["total_transactions"] == 1
    assert mB_summary["failed_count"] == 1


def test_discrepancies_duplicate_events(client: TestClient, make_event):
    """Test discrepancy detection for duplicate event types on a single transaction."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_dup_merchant"

    # Ingest happy path up to settled
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:01:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:02:00+00:00"))

    # Ingest a SECOND settled event with a DIFFERENT event_id
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:05:00+00:00"))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    dup_discrepancies = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "duplicate_event_type"
    ]
    assert len(dup_discrepancies) == 1
    assert "settled" in dup_discrepancies[0]["explanation"]


def test_discrepancies_invalid_transition(client: TestClient, make_event):
    """Test discrepancy detection for invalid state machine transitions."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_invalid_trans_merchant"

    # Ingest payment_processed then payment_initiated (invalid transition backward)
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:05:00+00:00"))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    invalid_trans = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "invalid_state_transition"
    ]
    assert len(invalid_trans) >= 1


def test_discrepancies_settled_without_processed(client: TestClient, make_event):
    """Test discrepancy detection when settled occurs without payment_processed."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_settled_no_proc_merchant"

    # Ingest payment_initiated directly followed by settled (missing payment_processed)
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:02:00+00:00"))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    target_disc = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "settled_without_payment_processed"
    ]
    assert len(target_disc) == 1


def test_discrepancies_payment_failed_after_settled(client: TestClient, make_event):
    """Test discrepancy detection when payment_failed is received after settled."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_failed_after_settled_merchant"

    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_initiated", timestamp="2026-01-10T10:00:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:01:00+00:00"))
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="settled", timestamp="2026-01-10T10:02:00+00:00"))

    # payment_failed occurs AFTER settled (at 10:05:00)
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_failed", timestamp="2026-01-10T10:05:00+00:00"))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    target_disc = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "payment_failed_after_settled"
    ]
    assert len(target_disc) == 1


def test_discrepancies_missing_payment_initiated(client: TestClient, make_event):
    """Test discrepancy detection when transaction is missing payment_initiated event."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_missing_initiated_merchant"

    # Direct payment_processed without prior payment_initiated
    client.post("/events", json=make_event(transaction_id=txn_id, merchant_id=m_id, event_type="payment_processed", timestamp="2026-01-10T10:00:00+00:00"))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    target_disc = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "missing_payment_initiated"
    ]
    assert len(target_disc) == 1


def test_discrepancies_processed_never_settled_stale(client: TestClient, make_event):
    """A transaction stuck in payment_processed past the threshold is flagged."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_stale_processed_merchant"

    # An event older than the default 24h threshold (relative to test-run time).
    client.post("/events", json=make_event(
        transaction_id=txn_id,
        merchant_id=m_id,
        event_type="payment_initiated",
        timestamp="2020-01-10T10:00:00+00:00",
    ))
    client.post("/events", json=make_event(
        transaction_id=txn_id,
        merchant_id=m_id,
        event_type="payment_processed",
        timestamp="2020-01-10T10:01:00+00:00",
    ))

    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    data = resp.json()

    target = [
        d for d in data["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "processed_never_settled"
    ]
    assert len(target) == 1
    assert "payment_processed" in target[0]["explanation"]


def test_discrepancies_processed_never_settled_flag_all(client: TestClient, make_event):
    """stale_after_hours=0 must flag EVERY processed-only transaction, regardless of age."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_zero_threshold_merchant"

    # Recent processed transaction — would normally not be flagged.
    client.post("/events", json=make_event(
        transaction_id=txn_id,
        merchant_id=m_id,
        event_type="payment_initiated",
    ))
    client.post("/events", json=make_event(
        transaction_id=txn_id,
        merchant_id=m_id,
        event_type="payment_processed",
    ))

    # Default threshold: not flagged.
    resp_default = client.get("/reconciliation/discrepancies")
    default_hits = [
        d for d in resp_default.json()["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "processed_never_settled"
    ]
    assert default_hits == []

    # threshold=0: flagged.
    resp_zero = client.get("/reconciliation/discrepancies?stale_after_hours=0")
    zero_hits = [
        d for d in resp_zero.json()["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "processed_never_settled"
    ]
    assert len(zero_hits) == 1


def test_discrepancies_processed_but_settled_not_flagged(client: TestClient, make_event):
    """A transaction that reached settled is never flagged as processed_never_settled."""
    txn_id = str(uuid.uuid4())
    m_id = "disc_settled_ok_merchant"

    # Old happy-path transaction — old enough to trip the stale check IF it were
    # still in payment_processed state.
    client.post("/events", json=make_event(
        transaction_id=txn_id, merchant_id=m_id,
        event_type="payment_initiated", timestamp="2020-01-10T10:00:00+00:00",
    ))
    client.post("/events", json=make_event(
        transaction_id=txn_id, merchant_id=m_id,
        event_type="payment_processed", timestamp="2020-01-10T10:01:00+00:00",
    ))
    client.post("/events", json=make_event(
        transaction_id=txn_id, merchant_id=m_id,
        event_type="settled", timestamp="2020-01-10T10:02:00+00:00",
    ))

    resp = client.get("/reconciliation/discrepancies?stale_after_hours=0")
    hits = [
        d for d in resp.json()["discrepancies"]
        if d["transaction_id"] == txn_id and d["discrepancy_type"] == "processed_never_settled"
    ]
    assert hits == []


def test_summary_group_by_status(client: TestClient, make_event):
    """group_by=status returns one row per lifecycle state with a uniform shape."""
    m_id = "merchant_group_by_status"

    # Two settled, one failed, one initiated-only.
    for i in range(2):
        txn = str(uuid.uuid4())
        client.post("/events", json=make_event(transaction_id=txn, merchant_id=m_id, event_type="payment_initiated", timestamp=f"2026-01-10T10:0{i}:00+00:00"))
        client.post("/events", json=make_event(transaction_id=txn, merchant_id=m_id, event_type="payment_processed", timestamp=f"2026-01-10T10:0{i}:01+00:00"))
        client.post("/events", json=make_event(transaction_id=txn, merchant_id=m_id, event_type="settled", timestamp=f"2026-01-10T10:0{i}:02+00:00"))

    txn_failed = str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=txn_failed, merchant_id=m_id, event_type="payment_initiated"))
    client.post("/events", json=make_event(transaction_id=txn_failed, merchant_id=m_id, event_type="payment_failed", timestamp="2026-01-10T10:05:00+00:00"))

    txn_pending = str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=txn_pending, merchant_id=m_id, event_type="payment_initiated"))

    resp = client.get("/reconciliation/summary?group_by=status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["group_by"] == "status"
    # No backward-compat merchants field on non-merchant grouping.
    assert data.get("merchants") is None

    by_status = {g["group_key"]: g for g in data["groups"]}
    # Only the ones we produced above must be present (test uses a clean DB per test).
    assert by_status["settled"]["total_transactions"] == 2
    assert by_status["payment_failed"]["total_transactions"] == 1
    assert by_status["payment_initiated"]["total_transactions"] == 1


def test_summary_group_by_date(client: TestClient, make_event):
    """group_by=date buckets transactions by the UTC date of initiated_at."""
    m_id = "merchant_group_by_date"

    # Two transactions on Jan 10, one on Jan 12.
    for i in range(2):
        client.post("/events", json=make_event(
            transaction_id=str(uuid.uuid4()), merchant_id=m_id,
            timestamp=f"2026-01-10T10:0{i}:00+00:00",
        ))
    client.post("/events", json=make_event(
        transaction_id=str(uuid.uuid4()), merchant_id=m_id,
        timestamp="2026-01-12T09:00:00+00:00",
    ))

    resp = client.get("/reconciliation/summary?group_by=date")
    assert resp.status_code == 200
    data = resp.json()
    assert data["group_by"] == "date"

    by_date = {g["group_key"]: g for g in data["groups"]}
    assert by_date["2026-01-10"]["total_transactions"] == 2
    assert by_date["2026-01-12"]["total_transactions"] == 1


def test_summary_group_by_merchant_backward_compat(client: TestClient, make_event):
    """Default (group_by=merchant) must keep emitting the legacy merchants/total_merchants fields."""
    m_id = "merchant_backward_compat"
    client.post("/events", json=make_event(
        transaction_id=str(uuid.uuid4()), merchant_id=m_id, merchant_name="Compat Corp",
    ))

    resp = client.get("/reconciliation/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["group_by"] == "merchant"
    assert data["total_merchants"] == 1
    assert data["merchants"][0]["merchant_id"] == m_id
    assert data["merchants"][0]["merchant_name"] == "Compat Corp"

    # New unified `groups` shape is also present.
    assert data["total_groups"] == 1
    assert data["groups"][0]["group_key"] == m_id
    assert data["groups"][0]["merchant_name"] == "Compat Corp"


def test_summary_group_by_invalid_rejected(client: TestClient):
    """Unknown group_by values are rejected with 422 by the enum whitelist."""
    resp = client.get("/reconciliation/summary?group_by=phase_of_moon")
    assert resp.status_code == 422
