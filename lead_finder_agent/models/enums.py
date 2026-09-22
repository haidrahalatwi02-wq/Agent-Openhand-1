"""Shared enumerations.

Values are plain strings so they serialize cleanly to JSON/CSV and stay stable
across versions.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum that compares equal to its raw value.

    ``enum.StrEnum`` only exists on Python 3.11+, so we define our own to keep
    support for 3.10.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class WebsiteStatus(StrEnum):
    """Outcome of a website check.

    ``UNKNOWN`` is deliberately distinct from ``NOT_FOUND``: an inconclusive
    check (network error, blocked request, timeout) must never be reported as
    "this business has no website".
    """

    EXISTS = "website_exists"
    NOT_FOUND = "website_not_found"
    UNREACHABLE = "website_unreachable"
    UNKNOWN = "website_unknown"
    NOT_CHECKED = "website_not_checked"


class WebsiteQuality(StrEnum):
    """Rough quality bucket for a website that was actually reachable."""

    GOOD = "good"
    WEAK = "weak"
    SOCIAL_ONLY = "social_only"
    UNKNOWN = "unknown"


class BusinessStatus(StrEnum):
    """Whether the business appears to still be operating."""

    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """Confidence bucket attached to a score."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LeadPriority(StrEnum):
    """Human friendly priority derived from the score."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DISQUALIFIED = "disqualified"


class ProviderKind(StrEnum):
    """How a search provider obtains its data."""

    API = "api"
    SAMPLE = "sample"
    CUSTOM = "custom"


__all__ = [
    "StrEnum",
    "WebsiteStatus",
    "WebsiteQuality",
    "BusinessStatus",
    "Confidence",
    "LeadPriority",
    "ProviderKind",
]
