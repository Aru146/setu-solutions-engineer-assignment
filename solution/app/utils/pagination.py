"""Pagination primitives shared across all list endpoints."""

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# ── Query parameters ──────────────────────────────────────────────────────────

# Maximum page size enforced server-side to prevent runaway queries.
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


class PaginationParams:
    """Reusable FastAPI dependency for offset pagination query params.

    Usage::

        @router.get("/items")
        def list_items(page: PaginationParams = Depends()):
            ...  page.limit, page.offset
    """

    def __init__(
        self,
        limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Number of results to return"),
        offset: int = Query(default=0, ge=0, description="Number of results to skip"),
    ) -> None:
        self.limit = limit
        self.offset = offset


# ── Response envelope ─────────────────────────────────────────────────────────


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated list envelope returned by all list endpoints."""

    items: list[T]
    total: int = Field(..., description="Total number of records matching the filters (ignoring limit/offset)")
    limit: int = Field(..., description="Page size used for this response")
    offset: int = Field(..., description="Offset used for this response")

    model_config = ConfigDict(from_attributes=True)
