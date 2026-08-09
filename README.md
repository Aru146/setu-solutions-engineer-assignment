# Payment Reconciliation Service

Lightweight, production-grade backend service built with Python, FastAPI, and PostgreSQL. It ingests asynchronous payment lifecycle events, tracks transaction states, computes merchant-level reconciliation metrics, and identifies reconciliation discrepancies.

---

## Table of Contents
- [1. Project Overview](#1-project-overview)
- [2. Problem Statement](#2-problem-statement)
- [3. Features Implemented](#3-features-implemented)
- [4. Tech Stack](#4-tech-stack)
- [5. Project Structure](#5-project-structure)
- [6. Architecture Overview](#6-architecture-overview)
- [7. Database Schema](#7-database-schema)
- [8. Event Lifecycle](#8-event-lifecycle)
- [9. API Endpoints](#9-api-endpoints)
- [10. Design Decisions](#10-design-decisions)
  - [Idempotency](#idempotency)
  - [Event Ordering](#event-ordering)
  - [Concurrency Handling](#concurrency-handling)
  - [Reconciliation Strategy](#reconciliation-strategy)
- [11. Running Locally](#11-running-locally)
- [12. Docker Setup](#12-docker-setup)
- [13. Environment Variables](#13-environment-variables)
- [14. Running Alembic Migrations](#14-running-alembic-migrations)
- [15. Seeding the Sample Dataset](#15-seeding-the-sample-dataset)
- [16. Running the Test Suite](#16-running-the-test-suite)
- [17. Postman Collection](#17-postman-collection)
- [18. Deployment](#18-deployment)
- [19. Assumptions](#19-assumptions)
- [20. Future Improvements](#20-future-improvements)

---

## 1. Project Overview

The **Payment Reconciliation Service** processes asynchronous event streams emitted by payment gateways and bank settlement systems. The system maintains transaction state, tracks complete event audit histories, and surfaces reconciliation anomalies to operations teams.

---

## 2. Problem Statement

Payment gateways and banking partners emit lifecycle events asynchronously. Due to network retries, out-of-order deliveries, and processing failures:
1. **Duplicate Events**: The same event payload may be submitted multiple times.
2. **Out-of-Order Delivery**: A `settled` or `payment_processed` event may arrive before `payment_initiated`.
3. **Data Discrepancies**: Transactions can be stuck in intermediate states, settle without processing, fail after settlement, or suffer missing initiation events.

This service solves these challenges by implementing an **append-only event log**, **idempotent event ingestion**, **out-of-order status guards**, and **dynamic multi-check discrepancy detection**.

---

## 3. Features Implemented

- **Idempotent Event Ingestion**: `POST /events` ingests payment events (`payment_initiated`, `payment_processed`, `payment_failed`, `settled`) safely without creating duplicate states. Duplicate `event_id`s return **HTTP 200** with the cached response; new events return **HTTP 201**.
- **Transaction State Tracking**: Materializes current status on transactions while preserving append-only event audit logs.
- **Automatic Merchant Provisioning**: Automatically provisions merchant records when new merchant events arrive.
- **Paginated, Filtered & Sortable Transaction Querying**: `GET /transactions` supports offset pagination (`limit`, `offset`), merchant filtering (`merchant_id`), status filtering (`current_status`), date range filtering (`start_date`, `end_date`), and configurable sorting via `sort_by` (`initiated_at` | `updated_at` | `amount` | `merchant_id`) and `order` (`asc` | `desc`).
- **Transaction Detail Lookup**: `GET /transactions/{transaction_id}` retrieves full transaction state and chronologically ordered event histories.
- **Reconciliation Aggregation Summary**: `GET /reconciliation/summary` computes counts per lifecycle status, grouped by the dimension specified via `group_by` (`merchant` | `date` | `status`). Backward-compatible: when `group_by=merchant` (the default), the response still includes the original `merchants` / `total_merchants` fields.
- **Dynamic Discrepancy Detection**: `GET /reconciliation/discrepancies` runs **6** distinct anomaly detection checks across all transactions:
  1. `duplicate_event_type`
  2. `missing_payment_initiated`
  3. `payment_failed_after_settled`
  4. `settled_without_payment_processed`
  5. `processed_never_settled` (configurable age threshold via `stale_after_hours` query param — default `24`; set to `0` to flag every processed-only transaction regardless of age)
  6. `invalid_state_transition`
- **Bulk Seeding Script**: `scripts/seed.py` ingests the assignment's 10K-event `sample_events.json` idempotently using batched `INSERT ... ON CONFLICT` — full load runs in a few seconds.
- **Database Migrations**: Alembic versioning for schema management.
- **Dockerized Environment**: Containerized startup via `Dockerfile` and `docker-compose.yml`.
- **Deployment Configs**: `fly.toml` and `render.yaml` ready for one-command deploys to Fly.io or Render.
- **Postman Collection**: End-to-end request examples under `postman/`.

---

## 4. Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 3.11+ |
| **Framework** | FastAPI 0.115+ |
| **Database** | PostgreSQL 16 (via `psycopg2-binary`) |
| **ORM** | SQLAlchemy 2.0 |
| **Data Validation** | Pydantic v2 |
| **Migrations** | Alembic 1.14+ |
| **Containerization** | Docker & Docker Compose |
| **Testing** | pytest, FastAPI `TestClient` |

---

## 5. Project Structure

```text
Backend/
├── alembic/
│   ├── env.py                  # Alembic environment setup
│   ├── script.py.mako          # Migration template
│   └── versions/
│       └── 0001_initial_schema.py # Initial DDL migration
├── app/
│   ├── __init__.py
│   ├── config.py               # Pydantic Settings configuration
│   ├── database.py             # SQLAlchemy engine & session factory
│   ├── logging_config.py      # Structured application logger setup
│   ├── main.py                 # FastAPI application factory & lifespan hooks
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── enums.py            # EventType and TransactionStatus Python enums
│   │   ├── event.py            # Event model (events table)
│   │   ├── merchant.py         # Merchant model (merchants table)
│   │   └── transaction.py      # Transaction model (transactions table)
│   ├── routers/                # FastAPI APIRouters
│   │   ├── events.py           # POST /events
│   │   ├── transactions.py     # GET /transactions & GET /transactions/{id}
│   │   └── reconciliation.py   # GET /reconciliation/summary & /discrepancies
│   ├── schemas/                # Pydantic request/response schemas
│   │   ├── event.py            # Event payload schemas
│   │   ├── reconciliation.py   # Reconciliation response schemas
│   │   └── transaction.py      # Transaction response schemas
│   ├── services/               # Core business logic layer
│   │   ├── discrepancy_service.py  # Anomaly detection checks
│   │   ├── event_service.py        # Event ingestion & state updates
│   │   ├── reconciliation_service.py # Summary aggregation logic
│   │   └── transaction_service.py  # Transaction querying & filters
│   └── utils/
│       └── pagination.py       # Offset/limit pagination params & envelopes
├── scripts/
│   └── seed.py                 # Data seeding script placeholder
├── tests/
│   ├── conftest.py             # Pytest fixtures & isolated DB setup
│   ├── test_events.py          # POST /events unit & integration tests
│   ├── test_reconciliation.py  # Summary & discrepancy tests
│   └── test_transactions.py    # Transaction list & detail tests
├── .env.example
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── requirements.txt
```

---

## 6. Architecture Overview

The application follows a clean layered architecture with strict separation of concerns:

```text
HTTP Request
     │
     ▼
┌─────────────────────────┐
│     FastAPI Routers     │  app/routers/
└────────────┬────────────┘
             │ Validates via Pydantic Schemas (app/schemas/)
             ▼
┌─────────────────────────┐
│     Service Layer       │  app/services/
└────────────┬────────────┘
             │ Manages business state, guards, & transactions
             ▼
┌─────────────────────────┐
│    SQLAlchemy ORM       │  app/models/
└────────────┬────────────┘
             │ Executes queries
             ▼
┌─────────────────────────┐
│  PostgreSQL Database    │  merchants, transactions, events
└─────────────────────────┘
```

---

## 7. Database Schema

The database consists of three core tables (`merchants`, `transactions`, `events`):

```mermaid
erDiagram
    merchants ||--o{ transactions : "has many"
    transactions ||--o{ events : "has many"

    merchants {
        varchar(50) merchant_id PK
        varchar(255) merchant_name
        timestamp_tz created_at
        timestamp_tz updated_at
    }

    transactions {
        uuid transaction_id PK
        varchar(50) merchant_id FK
        numeric(12,2) amount
        varchar(3) currency
        varchar(20) current_status
        timestamp_tz initiated_at
        timestamp_tz updated_at
    }

    events {
        uuid event_id PK
        varchar(20) event_type
        uuid transaction_id FK
        numeric(12,2) amount
        varchar(3) currency
        timestamp_tz event_timestamp
        timestamp_tz ingested_at
    }
```

### Table Definitions & Constraints

#### 1. `merchants`
- `merchant_id` (`VARCHAR(50)`, Primary Key)
- `merchant_name` (`VARCHAR(255)`, NOT NULL)
- `created_at` (`TIMESTAMPTZ`, default `now()`)
- `updated_at` (`TIMESTAMPTZ`, default `now()`)

#### 2. `transactions`
- `transaction_id` (`UUID`, Primary Key)
- `merchant_id` (`VARCHAR(50)`, FK → `merchants.merchant_id`)
- `amount` (`NUMERIC(12, 2)`, NOT NULL, Constraint: `amount > 0`)
- `currency` (`VARCHAR(3)`, NOT NULL, default `'INR'`)
- `current_status` (`VARCHAR(20)`, NOT NULL, Check Constraint: `payment_initiated`, `payment_processed`, `payment_failed`, `settled`)
- `initiated_at` (`TIMESTAMPTZ`, NOT NULL)
- `updated_at` (`TIMESTAMPTZ`, default `now()`)
- **Indexes**:
  - `ix_txn_merchant_status` on `(merchant_id, current_status)` — composite index; also satisfies merchant_id-only filters via a prefix scan
  - `ix_txn_initiated` on `(initiated_at)` — supports date-range filters and the default `ORDER BY initiated_at DESC` sort (PostgreSQL scans B-tree indexes backwards natively, so no explicit `DESC` in the DDL is required)

#### 3. `events`
- `event_id` (`UUID`, Primary Key — Idempotency Key)
- `event_type` (`VARCHAR(20)`, NOT NULL, Check Constraint: `payment_initiated`, `payment_processed`, `payment_failed`, `settled`)
- `transaction_id` (`UUID`, FK → `transactions.transaction_id` ON DELETE CASCADE)
- `amount` (`NUMERIC(12, 2)`, NOT NULL, Constraint: `amount > 0`)
- `currency` (`VARCHAR(3)`, NOT NULL, default `'INR'`)
- `event_timestamp` (`TIMESTAMPTZ`, NOT NULL — Business clock)
- `ingested_at` (`TIMESTAMPTZ`, default `now()` — Server clock)
- **Indexes**:
  - `ix_evt_transaction` on `(transaction_id, event_timestamp)`

---

## 8. Event Lifecycle

A normal payment lifecycle moves through the following valid state machine:

```text
               ┌───────────────────────┐
               │   payment_initiated   │
               └───────────┬───────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
┌─────────────────────────┐  ┌───────────────────────┐
│    payment_processed    │  │    payment_failed     │ (Terminal)
└────────────┬────────────┘  └───────────────────────┘
             │
             ├──────────────────────────┐
             ▼                          ▼
┌─────────────────────────┐  ┌───────────────────────┐
│         settled         │  │    payment_failed     │ (Terminal)
└─────────────────────────┘  └───────────────────────┘
       (Terminal)
```

Transitions outside this path (e.g., `settled` before `payment_processed`, or multiple `settled` events) trigger reconciliation discrepancies.

---

## 9. API Endpoints

### Health Check
- `GET /health`: Liveness probe returning `{"status": "healthy"}`.

### 1. Ingest Event
`POST /events`
- **Request Body**:
```json
{
  "event_id": "ef4f7497-6469-4734-9760-ce3bc2e9d66a",
  "event_type": "payment_initiated",
  "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
  "merchant_id": "merchant_1",
  "merchant_name": "Acme Corp",
  "amount": 1500.00,
  "currency": "INR",
  "timestamp": "2026-01-10T10:00:00+00:00"
}
```
- **Response (`201 Created` for new events / `200 OK` for duplicates)**:
```json
{
  "success": true,
  "message": "Event ingested successfully",
  "event": {
    "event_id": "ef4f7497-6469-4734-9760-ce3bc2e9d66a",
    "event_type": "payment_initiated",
    "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
    "merchant_id": "merchant_1",
    "amount": 1500.00,
    "currency": "INR",
    "event_timestamp": "2026-01-10T10:00:00Z",
    "ingested_at": "2026-01-10T10:00:01Z"
  },
  "transaction": {
    "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
    "merchant_id": "merchant_1",
    "current_status": "payment_initiated",
    "amount": 1500.00,
    "currency": "INR",
    "initiated_at": "2026-01-10T10:00:00Z"
  }
}
```

### 2. List Transactions
`GET /transactions`
- **Query Parameters**:
  - `merchant_id` — exact-match filter (e.g. `merchant_1`)
  - `current_status` — one of `payment_initiated`, `payment_processed`, `payment_failed`, `settled`
  - `start_date`, `end_date` — ISO-8601 datetimes (inclusive; naive values treated as UTC)
  - `sort_by` — one of `initiated_at` (default), `updated_at`, `amount`, `merchant_id`
  - `order` — `asc` or `desc` (default `desc`)
  - `limit` — page size, default 20, max 100
  - `offset` — pagination offset, default 0
- **Response (`200 OK`)**:
```json
{
  "items": [
    {
      "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
      "merchant_id": "merchant_1",
      "amount": 1500.00,
      "currency": "INR",
      "current_status": "settled",
      "initiated_at": "2026-01-10T10:00:00Z",
      "updated_at": "2026-01-10T10:02:00Z"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

### 3. Get Transaction Detail
`GET /transactions/{transaction_id}`
- **Response (`200 OK`)**:
```json
{
  "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
  "merchant_id": "merchant_1",
  "merchant_name": "Acme Corp",
  "amount": 1500.00,
  "currency": "INR",
  "current_status": "settled",
  "initiated_at": "2026-01-10T10:00:00Z",
  "updated_at": "2026-01-10T10:02:00Z",
  "events": [
    {
      "event_id": "ef4f7497-6469-4734-9760-ce3bc2e9d66a",
      "event_type": "payment_initiated",
      "amount": 1500.00,
      "currency": "INR",
      "event_timestamp": "2026-01-10T10:00:00Z"
    },
    {
      "event_id": "ab897497-6469-4734-9760-ce3bc2e9d66b",
      "event_type": "settled",
      "amount": 1500.00,
      "currency": "INR",
      "event_timestamp": "2026-01-10T10:02:00Z"
    }
  ]
}
```
- **Error (`404 Not Found`)**: When `transaction_id` does not exist.

### 4. Reconciliation Summary Report
`GET /reconciliation/summary`
- **Query Parameters**:
  - `group_by` — one of `merchant` (default), `date`, `status`
- **Response (`200 OK`)** — the `groups` array is uniform across all three modes; the legacy `merchants` / `total_merchants` fields are also returned when `group_by=merchant`:
```json
{
  "group_by": "merchant",
  "groups": [
    {
      "group_key": "merchant_1",
      "merchant_name": "Acme Corp",
      "total_transactions": 10,
      "payment_initiated_count": 2,
      "payment_processed_count": 3,
      "settled_count": 4,
      "failed_count": 1
    }
  ],
  "total_groups": 1,
  "merchants": [
    {
      "merchant_id": "merchant_1",
      "merchant_name": "Acme Corp",
      "total_transactions": 10,
      "payment_initiated_count": 2,
      "payment_processed_count": 3,
      "settled_count": 4,
      "failed_count": 1
    }
  ],
  "total_merchants": 1
}
```

When `group_by=date`, each `group_key` is an ISO-8601 date (e.g. `"2026-01-10"`); when `group_by=status`, each `group_key` is a lifecycle state name.

### 5. Reconciliation Discrepancies Report
`GET /reconciliation/discrepancies`
- **Response (`200 OK`)**:
```json
{
  "discrepancies": [
    {
      "transaction_id": "02d878de-f807-4bf1-9b16-7098be6e54fe",
      "merchant_id": "merchant_1",
      "merchant_name": "Acme Corp",
      "current_status": "settled",
      "discrepancy_type": "settled_without_payment_processed",
      "explanation": "Transaction reached 'settled' without a 'payment_processed' event occurring before it — an expected intermediate step.",
      "event_history": [
        {
          "event_id": "ef4f7497-6469-4734-9760-ce3bc2e9d66a",
          "event_type": "payment_initiated",
          "amount": 1500.00,
          "currency": "INR",
          "event_timestamp": "2026-01-10T10:00:00Z"
        },
        {
          "event_id": "ab897497-6469-4734-9760-ce3bc2e9d66b",
          "event_type": "settled",
          "amount": 1500.00,
          "currency": "INR",
          "event_timestamp": "2026-01-10T10:02:00Z"
        }
      ]
    }
  ],
  "total_discrepancies": 1
}
```

---

## 10. Design Decisions

### Idempotency
- `event_id` is defined as the Primary Key on the `events` table.
- When an event with an existing `event_id` arrives, `event_service` intercepts the DB query or catches unique constraint violations (`pk_events` / `events_pkey`), performs a rollback, and returns the cached event and transaction snapshot with HTTP `200 OK`.

### Event Ordering
- Events may arrive out of order.
- To prevent regressions, `event_service` queries `MAX(event_timestamp)` for existing transaction events before updating `current_status`.
- If an incoming event is older than the latest recorded event timestamp, the event is persisted into the append-only log, but `transaction.current_status` is not reverted to an earlier state.

### Concurrency Handling
- Merchant provisioning uses PostgreSQL `INSERT ... ON CONFLICT (merchant_id) DO UPDATE` to prevent race conditions when concurrent threads process events for a newly introduced merchant.
- Database writes execute within an atomic transaction block.

### Reconciliation Strategy
- `reconciliation_service.py` aggregates stats using SQL conditional counts (`func.count().filter(...)`) grouped by merchant.
- `discrepancy_service.py` fetches transactions and event streams using a 2-query batch fetch to avoid N+1 query overhead. Discrepancy checks evaluate event history in Python against defined state machine rules.

---

## 11. Running Locally

### Prerequisites
- Python 3.11+
- PostgreSQL 14+ running locally (or via Docker Compose — see next section)

### Setup Steps (macOS / Linux)

1. **Clone and enter the repository**:
   ```bash
   git clone https://github.com/Aru146/setu-solutions-engineer-assignment.git
   cd setu-solutions-engineer-assignment
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables** — copy `.env.example` to `.env` and edit as needed:
   ```bash
   cp .env.example .env
   ```

5. **Run database migrations**:
   ```bash
   alembic upgrade head
   ```

6. **(Optional) Seed the 10K sample events** — see [§15](#15-seeding-the-sample-dataset).

7. **Start the FastAPI application**:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

Interactive API docs at http://localhost:8000/docs, ReDoc at http://localhost:8000/redoc.

### Setup Steps (Windows PowerShell)

Same order as above; substitute the venv activation and copy commands:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

Everything else (`pip`, `alembic`, `uvicorn`) is identical on all platforms.

---

## 12. Docker Setup

Run the application and PostgreSQL database together using Docker Compose:

```bash
docker-compose up --build
```

This starts:
- **PostgreSQL container**: Listening on port `5432` (`payment_recon` database).
- **App container**: Runs Alembic migrations automatically on startup and starts Uvicorn on port `8000`.

To stop the containers:
```bash
docker-compose down
```

To wipe the database volume too:
```bash
docker-compose down -v
```

---

## 13. Environment Variables

Supported environment variables (read from environment or `.env` file):

| Variable | Default Value | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/payment_recon` | PostgreSQL connection string |
| `APP_ENV` | `development` | Application environment (`development`, `production`, `test`) |
| `APP_DEBUG` | `false` | Enables SQL query logging when `true` |
| `APP_HOST` | `0.0.0.0` | Bind host address |
| `APP_PORT` | `8000` | Bind port number (fallback when `PORT` isn't set) |
| `PORT` | *(unset)* | Injected by Fly.io / Render / Railway; wins over `APP_PORT` when present |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DEFAULT_PAGE_SIZE` | `20` | Default pagination limit |
| `MAX_PAGE_SIZE` | `100` | Maximum allowable pagination limit |

---

## 14. Running Alembic Migrations

- **Apply all pending migrations**:
  ```bash
  alembic upgrade head
  ```

- **Roll back the last migration**:
  ```bash
  alembic downgrade -1
  ```

- **Generate a new migration script**:
  ```bash
  alembic revision --autogenerate -m "description_of_changes"
  ```

---

## 15. Seeding the Sample Dataset

The assignment's [`sample_events.json`](https://github.com/SetuHQ/hiring-assignments/blob/main/solutions-engineer/sample_events.json) (~10,000 events, 5 merchants, 3 months of activity) is bundled at `data/sample_events.json`. Load it into your local database with:

```bash
# Ensure migrations have run first
alembic upgrade head

# Bulk-seed the events (few seconds for 10K rows)
python -m scripts.seed
```

The seeder is **fully idempotent** — it uses PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` for events and `ON CONFLICT DO UPDATE` for merchants/transactions, so re-running it is safe. Each run reports how many events were newly inserted vs. already present.

Useful flags:

```bash
# Point at a different JSON file
python -m scripts.seed path/to/other_events.json

# Custom batch size (default 1000)
python -m scripts.seed --batch-size 500

# Verbose per-batch logging
python -m scripts.seed -v

# Wipe existing rows before seeding (local demo resets only)
python -m scripts.seed --truncate
```

After seeding, verify:

```bash
curl -s http://localhost:8000/reconciliation/summary | jq '.total_groups, .merchants[0]'
curl -s "http://localhost:8000/transactions?limit=3" | jq '.total, .items[0]'
```

---

## 16. Running the Test Suite

The test suite runs against an isolated PostgreSQL database (`payment_recon_test`) configured via `TEST_DATABASE_URL` in `tests/conftest.py`. Every test truncates the tables before running, so tests never leak state into each other.

Run pytest from the repository root:

```bash
python -m pytest tests/
```

Run tests with verbose output:

```bash
python -m pytest tests/ -v
```

Run a single test file / test function:

```bash
python -m pytest tests/test_events.py -v
python -m pytest tests/test_events.py::test_event_duplicate_idempotency -v
```

---

## 17. Postman Collection

A ready-to-import Postman collection is at [`postman/PaymentReconciliation.postman_collection.json`](postman/PaymentReconciliation.postman_collection.json). It exercises every endpoint, including:

- Happy-path event ingestion sequence (`payment_initiated` → `payment_processed` → `settled`).
- Idempotent replay of the same `event_id` (expect HTTP 200, no state change).
- Validation error case (unknown `event_type` → HTTP 422).
- Sort/paginate `GET /transactions`.
- All three `group_by` modes for `/reconciliation/summary`.
- `stale_after_hours=0` to force the `processed_never_settled` check to flag every processed-only transaction.

The collection defines a `baseUrl` variable — point it at `http://localhost:8000` for local dev or at your deployed URL.

---

## 18. Deployment

Two ready-to-use deployment configs are included; both consume the same `Dockerfile`, which honors the `PORT` env var injected by every major PaaS.

### Option A — Fly.io (`fly.toml`)

```bash
# One-time setup
brew install flyctl                             # or see https://fly.io/docs/hands-on/install-flyctl/
fly auth login
fly postgres create --name payment-recon-db     # smallest development plan
fly launch --no-deploy --copy-config            # accepts the app name from fly.toml
fly postgres attach payment-recon-db            # sets DATABASE_URL secret automatically

# Deploy
fly deploy
```

`alembic upgrade head` runs automatically on every container start (via the Dockerfile CMD), so schema migrations apply on each deploy. `/health` is wired to Fly's HTTP checks.

### Option B — Render (`render.yaml` Blueprint)

1. Push this repo to your own GitHub account.
2. In Render, click **New +** → **Blueprint** and point it at your fork.
3. Render detects `render.yaml`, provisions the managed Postgres, and wires `DATABASE_URL` into the web service automatically.
4. Click **Apply**.

### Post-deploy: seed the production database

The seeder connects with whatever `DATABASE_URL` is set. To seed a live database from your laptop:

```bash
# For Fly.io
DATABASE_URL="$(fly postgres connect --app payment-recon-db --command 'SELECT ...')" \
    python -m scripts.seed --truncate

# Or run it inside the container
fly ssh console -C "python -m scripts.seed"
```

For Render, use the Shell tab in the service dashboard and run `python -m scripts.seed`.

## 19. Assumptions

1. **Currency**: Payments default to `'INR'` unless explicitly specified.
2. **Amounts**: Currency amounts are positive decimal numbers stored with 2 decimal places (`NUMERIC(12, 2)`).
3. **Identification**: `event_id` and `transaction_id` are UUID strings. `merchant_id` is a string identifier up to 50 characters.
4. **Timezones**: Incoming timestamps are parsed and stored as UTC (`TIMESTAMPTZ`). Naive datetimes on filter query params are treated as UTC.
5. **Stale-processed threshold**: The default 24-hour window for `processed_never_settled` is a heuristic. Real deployments would tune this per merchant or per settlement channel.
6. **Bulk-seed semantics**: The seed script upserts the transaction's `current_status` from the latest event and `initiated_at` from the earliest event across the file, deriving state deterministically rather than replaying events one-by-one.

---

## 20. Future Improvements

1. **Async Database Driver**: Migrate SQLAlchemy sessions to `asyncpg` for high-throughput async DB queries.
2. **Keyset (Cursor-based) Pagination**: Cursor pagination for faster deep pagination on ultra-large datasets.
3. **Push-based Discrepancy Feed**: Instead of an on-demand `/reconciliation/discrepancies` scan, evaluate incrementally on each event ingest and emit into a work queue.
4. **Redis Ingestion Caching**: Cache recent `event_id` keys in Redis for high-speed idempotency pre-checks prior to hitting Postgres.
5. **Materialised daily aggregates**: For the `group_by=date` summary, back the query with a materialised view refreshed hourly to keep dashboard load O(1) as history grows.
6. **Per-merchant discrepancy thresholds**: Allow `stale_after_hours` to be configured per merchant instead of a single global value.
