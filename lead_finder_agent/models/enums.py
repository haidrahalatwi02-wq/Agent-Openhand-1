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


class WebsiteErrorKind(StrEnum):
    """Machine-readable reason a website check could not conclude.

    Callers branch on these instead of parsing message text, and the CLI maps
    them to a short human phrase so raw exception details never reach a user.
    """

    INVALID_URL = "invalid_url"
    TIMEOUT = "timeout"
    DNS_FAILURE = "dns_failure"
    TLS_FAILURE = "tls_failure"
    CONNECTION_FAILURE = "connection_failure"
    HTTP_ERROR = "http_error"
    REDIRECT_LIMIT = "redirect_limit"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    UNKNOWN = "unknown_error"


class WebsiteIdentity(StrEnum):
    """How strongly a reachable page can be tied to the business.

    Separate from :class:`WebsiteStatus` on purpose. Status answers "can we
    reach a website?", identity answers "is it plausibly *this* business's?".
    Folding the two together would either overstate ownership or downgrade a
    perfectly reachable site, so both are reported and neither is invented.
    """

    PROVIDED = "provided_by_source"
    TITLE_MATCH = "title_match"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


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
    "WebsiteErrorKind",
    "WebsiteIdentity",
    "BusinessStatus",
    "Confidence",
    "LeadPriority",
    "ProviderKind",
]
