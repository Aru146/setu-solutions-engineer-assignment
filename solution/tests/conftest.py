"""
Pytest configuration and shared fixtures.

Isolation strategy
------------------
- A separate PostgreSQL database (payment_recon_test) is created once per
  session so tests never touch the development database.
- All tables are created via Base.metadata.create_all() — no Alembic needed
  in tests since the ORM models are the source of truth.
- Every table is TRUNCATED before each test function (autouse), giving each
  test a completely clean slate.
- The FastAPI get_db dependency is overridden in every client fixture to use
  the test database, so the app code under test runs against real PostgreSQL.

Why a real database?
--------------------
The most critical correctness properties in this service — idempotency via PK
constraints, status progression guards, and discrepancy detection via SQL
aggregation — cannot be meaningfully tested with a mocked database.  Mocking
the DB would reduce the tests to checking that Python code calls certain
functions in a certain order, not that the system behaves correctly end-to-end.
"""

import os
import threading
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app

# ── Test database URL ─────────────────────────────────────────────────────────

TEST_DATABASE_URL: str = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/payment_recon_test",
)

# ── Session-scoped engine ─────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_engine():
    """
    Create the test database (if absent) and all schema tables.
    Runs exactly once per pytest session; drops tables on teardown.
    """
    # Create the test DB against the system 'postgres' database.
    root_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    root_engine = create_engine(root_url, isolation_level="AUTOCOMMIT")
    with root_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": db_name},
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    root_engine.dispose()

    engine = create_engine(TEST_DATABASE_URL, echo=False)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


# ── Per-test isolation ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    """
    Truncate all data tables before every test function.
    CASCADE handles FK ordering automatically; RESTART IDENTITY is a no-op
    for UUID PKs but ensures sequences reset if any are added in future.
    """
    with test_engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE events, transactions, merchants"
                " RESTART IDENTITY CASCADE"
            )
        )
        conn.commit()


# ── Database session fixture ──────────────────────────────────────────────────


@pytest.fixture
def db_session(test_engine) -> Generator[Session, None, None]:
    """Direct test database session for asserting persisted state after requests."""
    factory = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


# ── HTTP client fixture ───────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine) -> Generator[TestClient, None, None]:
    """
    FastAPI TestClient with get_db overridden to use the test database.
    Dependency overrides are cleared after each test to prevent leakage.
    """
    factory = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)

    def _override_get_db() -> Generator[Session, None, None]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Payload factory ───────────────────────────────────────────────────────────


def _event_payload(**overrides) -> dict:
    """
    Return a valid POST /events payload.
    Every call generates a fresh event_id and transaction_id unless overridden,
    so tests that need a specific ID must pass it explicitly.
    """
    base: dict = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_test_1",
        "merchant_name": "Test Merchant One",
        "amount": "100.00",
        "currency": "INR",
        "timestamp": "2026-01-10T10:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_event():
    """Fixture that exposes the event payload factory to test functions."""
    return _event_payload


@pytest.fixture
def ingest(client, make_event):
    """
    Helper: POST /events, assert 201, return (response_body, payload).
    Tests that need multi-event transactions should pass transaction_id explicitly.
    """

    def _ingest(**overrides) -> tuple[dict, dict]:
        payload = make_event(**overrides)
        resp = client.post("/events", json=payload)
        assert resp.status_code == 201, (
            f"Expected 201 from ingest helper, got {resp.status_code}: {resp.text}"
        )
        return resp.json(), payload

    return _ingest
