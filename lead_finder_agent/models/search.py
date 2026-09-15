"""Search request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from lead_finder_agent.models.enums import ProviderKind


def _location_resolver():
    """The shared :class:`LocationResolver`, or ``None`` when unavailable.

    Deferred to call time so importing the models package never triggers a
    config-file read, and cached inside the config module.
    """
    try:
        from lead_finder_agent.config.locations import get_location_resolver

        return get_location_resolver()
    except Exception:  # noqa: BLE001 - resolution is an enhancement, never a blocker
        return None


def reset_location_resolver() -> None:
    """Drop the cached resolver (used when configuration changes in tests)."""
    try:
        from lead_finder_agent.config.locations import (
            reset_location_resolver as _reset,
        )

        _reset()
    except Exception:  # noqa: BLE001 - nothing cached when config is unimportable
        pass


@dataclass
class SearchQuery:
    """Input to the search stage."""

    business_type: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    city: Optional[str] = None
    country: Optional[str] = None
    limit: int = 50
    providers: Optional[List[str]] = None
    language: str = "en"

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(self.keywords, str):
            self.keywords = [k.strip() for k in self.keywords.split(",") if k.strip()]
        self.keywords = [k.strip() for k in (self.keywords or []) if k and k.strip()]
        if self.business_type:
            self.business_type = self.business_type.strip()
        self._normalize_location()

    def _normalize_location(self) -> None:
        """Fold ``عدن``/``مدينة عدن``/``Aden`` onto one canonical spelling."""
        resolver = _location_resolver()
        if resolver is None:  # pragma: no cover - defensive
            return

        canonical_city, inferred_country = resolver.resolve_city(self.city)
        if canonical_city:
            self.city = canonical_city
        if self.country:
            self.country = resolver.resolve_country(self.country)
        elif inferred_country:
            # Only fill in a country the user left blank; never override input.
            self.country = inferred_country

    @property
    def location(self) -> str:
        parts = [p for p in (self.city, self.country) if p]
        return ", ".join(parts)

    def cache_key(self) -> str:
        providers = ",".join(sorted(self.providers or []))
        keywords = ",".join(sorted(self.keywords))
        return (
            f"{self.business_type}|{keywords}|{self.city}|{self.country}|"
            f"{self.limit}|{providers}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "business_type": self.business_type,
            "keywords": list(self.keywords),
            "city": self.city,
            "country": self.country,
            "limit": self.limit,
            "providers": list(self.providers or []),
            "language": self.language,
        }


@dataclass
class ProviderResponse:
    """What a single provider returns."""

    provider: str
    kind: ProviderKind = ProviderKind.API
    leads: List[Any] = field(default_factory=list)
    error: Optional[str] = None
    skipped_reason: Optional[str] = None
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def count(self) -> int:
        return len(self.leads)


@dataclass
class SearchResult:
    """Aggregate result of the search stage across all providers."""

    query: SearchQuery
    leads: List[Any] = field(default_factory=list)
    responses: List[ProviderResponse] = field(default_factory=list)

    @property
    def errors(self) -> Dict[str, str]:
        return {r.provider: r.error for r in self.responses if r.error}

    @property
    def providers_used(self) -> List[str]:
        return [r.provider for r in self.responses if r.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "leads": [lead.to_dict() for lead in self.leads],
            "providers": [
                {
                    "provider": r.provider,
                    "kind": str(r.kind),
                    "count": r.count,
                    "error": r.error,
                    "skipped_reason": r.skipped_reason,
                    "elapsed_seconds": round(r.elapsed_seconds, 4),
                }
                for r in self.responses
            ],
        }


__all__ = ["SearchQuery", "ProviderResponse", "SearchResult", "reset_location_resolver"]