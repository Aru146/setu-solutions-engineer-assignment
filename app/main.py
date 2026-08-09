"""FastAPI application factory and lifespan management."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup and shutdown lifecycle hooks."""
    # ── Startup ──────────────────────────────────────────────────────
    setup_logging()
    logger.info(
        "Starting Payment Reconciliation Service (env=%s, debug=%s)",
        settings.app_env,
        settings.app_debug,
    )
    yield
    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down Payment Reconciliation Service")


def create_app() -> FastAPI:
    """Build and return the FastAPI application instance."""
    application = FastAPI(
        title="Payment Reconciliation Service",
        description=(
            "Lightweight backend service for payment event ingestion, "
            "transaction tracking, and reconciliation reporting."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── Health check ─────────────────────────────────────────────────
    @application.get("/health", tags=["System"])
    def health_check():
        """Liveness probe for load balancers and container orchestrators."""
        return {"status": "healthy"}

    # ── Register routers ─────────────────────────────────────────────
    from app.routers import events, transactions, reconciliation  # noqa: PLC0415

    application.include_router(events.router)
    application.include_router(transactions.router)
    application.include_router(reconciliation.router)

    return application


# Module-level app instance used by uvicorn (``uvicorn app.main:app``)
app = create_app()
