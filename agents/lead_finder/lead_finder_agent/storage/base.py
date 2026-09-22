"""Storage interface.

Swapping SQLite for Postgres later means writing one new class that satisfies
this interface; nothing in the pipeline needs to change.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from lead_finder_agent.models import Lead, WebsiteStatus


@dataclass
class LeadFilter:
    """Criteria for querying stored leads."""

    city: Optional[str] = None
    country: Optional[str] = None
    business_type: Optional[str] = None
    source: Optional[str] = None
    website_status: Optional[WebsiteStatus] = None
    min_score: Optional[int] = None
    max_score: Optional[int] = None
    priority: Optional[str] = None
    has_phone: Optional[bool] = None
    order_by: str = "lead_score"
    descending: bool = True
    limit: Optional[int] = None
    offset: int = 0

    #: Columns that may be used for ordering (guards against SQL injection).
    ALLOWED_ORDER = (
        "lead_score",
        "discovered_at",
        "last_checked_at",
        "business_name",
        "city",
        "priority",
    )

    def resolved_order(self) -> str:
        column = self.order_by if self.order_by in self.ALLOWED_ORDER else "lead_score"
        return f"{column} {'DESC' if self.descending else 'ASC'}"


class BaseLeadRepository(abc.ABC):
    """Persistence contract for :class:`Lead` objects."""

    @abc.abstractmethod
    def add(self, lead: Lead) -> Lead:
        """Insert a lead, or update the existing record with the same key."""

    @abc.abstractmethod
    def add_many(self, leads: Iterable[Lead]) -> int:
        """Insert many leads, returning the number written."""

    @abc.abstractmethod
    def get(self, lead_id: str) -> Optional[Lead]:
        """Fetch a lead by id."""

    @abc.abstractmethod
    def exists(self, lead: Lead) -> bool:
        """Whether an equivalent lead is already stored."""

    @abc.abstractmethod
    def find(self, filters: Optional[LeadFilter] = None) -> List[Lead]:
        """Query leads."""

    @abc.abstractmethod
    def count(self) -> int:
        """Total number of stored leads."""

    @abc.abstractmethod
    def delete(self, lead_id: str) -> bool:
        """Delete one lead."""

    @abc.abstractmethod
    def clear(self) -> None:
        """Delete every lead (used by tests)."""

    # -- optional convenience ---------------------------------------------

    def upsert_many(self, leads: Sequence[Lead]) -> int:
        """Alias for :meth:`add_many`, kept for readability at call sites."""
        return self.add_many(leads)

    def __enter__(self) -> "BaseLeadRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        close = getattr(self, "close", None)
        if callable(close):
            close()


__all__ = ["BaseLeadRepository", "LeadFilter"]
