"""Typed data models.

Everything is a dataclass with explicit ``to_dict`` / ``from_dict`` helpers so
the storage layer and exporters do not need to know about the classes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from lead_finder_agent.models.enums import (
    BusinessStatus,
    Confidence,
    LeadPriority,
    WebsiteQuality,
    WebsiteStatus,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_NON_WORD = re.compile(r"[^\w\s\-']", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def isoformat(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to ISO-8601 UTC text (``None`` passes through)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any) -> Optional[datetime]:
    """Best-effort parse of a datetime coming from storage or JSON."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_name(value: Optional[str]) -> str:
    """Normalize a business name for comparison and hashing."""
    if not value:
        return ""
    text = _NON_WORD.sub(" ", str(value).lower())
    return _WHITESPACE.sub(" ", text).strip()


def normalize_phone(value: Optional[str]) -> str:
    """Keep digits only, so ``+967 71 234-5678`` == ``967712345678``."""
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def normalize_url(value: Optional[str]) -> Optional[str]:
    """Add a scheme when missing and strip trailing slashes."""
    if not value:
        return None
    url = str(value).strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    return url.rstrip("/")


# --------------------------------------------------------------------------- #
# Website check
# --------------------------------------------------------------------------- #


@dataclass
class WebsiteCheckResult:
    """Result of inspecting a single business for an online presence."""

    status: WebsiteStatus = WebsiteStatus.NOT_CHECKED
    website_url: Optional[str] = None
    http_status: Optional[int] = None
    quality: WebsiteQuality = WebsiteQuality.UNKNOWN
    has_https: bool = False
    is_reachable: bool = False
    has_shop: bool = False
    has_contact_page: bool = False
    social_only: bool = False
    checked_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["quality"] = str(self.quality)
        data["checked_at"] = isoformat(self.checked_at)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebsiteCheckResult":
        payload = dict(data or {})
        payload["status"] = WebsiteStatus(payload.get("status") or WebsiteStatus.NOT_CHECKED)
        payload["quality"] = WebsiteQuality(payload.get("quality") or WebsiteQuality.UNKNOWN)
        payload["checked_at"] = parse_datetime(payload.get("checked_at"))
        payload.setdefault("notes", [])
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass
class LeadScore:
    """Score + confidence + human readable reasons."""

    score: int = 0
    confidence: Confidence = Confidence.LOW
    reasons: List[str] = field(default_factory=list)
    priority: LeadPriority = LeadPriority.COLD
    breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "confidence": str(self.confidence),
            "reasons": list(self.reasons),
            "priority": str(self.priority),
            "breakdown": dict(self.breakdown),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LeadScore":
        payload = dict(data or {})
        return cls(
            score=int(payload.get("score") or 0),
            confidence=Confidence(payload.get("confidence") or Confidence.LOW),
            reasons=list(payload.get("reasons") or []),
            priority=LeadPriority(payload.get("priority") or LeadPriority.COLD),
            breakdown={k: int(v) for k, v in (payload.get("breakdown") or {}).items()},
        )


# --------------------------------------------------------------------------- #
# Lead
# --------------------------------------------------------------------------- #


@dataclass
class Lead:
    """A single prospective customer.

    Only publicly available business data belongs here. Do not add personal
    data (home addresses, private phone numbers, ...) to this model.
    """

    business_name: str
    id: Optional[str] = None
    business_type: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    website_url: Optional[str] = None
    website_status: WebsiteStatus = WebsiteStatus.NOT_CHECKED
    website_quality: WebsiteQuality = WebsiteQuality.UNKNOWN
    website_checked_at: Optional[datetime] = None
    social_links: Dict[str, str] = field(default_factory=dict)
    description: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    business_status: BusinessStatus = BusinessStatus.UNKNOWN
    review_count: Optional[int] = None
    rating: Optional[float] = None
    categories: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    lead_score: int = 0
    score_confidence: Confidence = Confidence.LOW
    score_reason: List[str] = field(default_factory=list)
    score_breakdown: Dict[str, int] = field(default_factory=dict)
    priority: LeadPriority = LeadPriority.COLD
    discovered_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    dedupe_key: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def __post_init__(self) -> None:
        self.business_name = (self.business_name or "").strip()
        self.website_status = WebsiteStatus(self.website_status)
        self.website_quality = WebsiteQuality(self.website_quality)
        self.business_status = BusinessStatus(self.business_status)
        self.score_confidence = Confidence(self.score_confidence)
        self.priority = LeadPriority(self.priority)
        self.discovered_at = self.discovered_at or utcnow()
        if not self.dedupe_key:
            self.dedupe_key = self.compute_dedupe_key()
        if not self.id:
            self.id = self.dedupe_key

    # -- identity ----------------------------------------------------------

    def compute_dedupe_key(self) -> str:
        """Stable identity for a lead.

        Prefers an explicit ``source:source_id`` pair when the provider gives
        one, otherwise falls back to name + city + phone.
        """
        source_id = str(self.raw.get("source_id") or "").strip()
        if self.source and source_id:
            basis = f"{self.source}:{source_id}"
        else:
            basis = "|".join(
                [
                    normalize_name(self.business_name),
                    normalize_name(self.city),
                    normalize_phone(self.phone),
                ]
            )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    # -- scoring -----------------------------------------------------------

    def apply_score(self, result: LeadScore) -> "Lead":
        """Copy a :class:`LeadScore` onto the lead."""
        self.lead_score = result.score
        self.score_confidence = result.confidence
        self.score_reason = list(result.reasons)
        self.score_breakdown = dict(result.breakdown)
        self.priority = result.priority
        return self

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["website_status"] = str(self.website_status)
        data["website_quality"] = str(self.website_quality)
        data["business_status"] = str(self.business_status)
        data["score_confidence"] = str(self.score_confidence)
        data["priority"] = str(self.priority)
        data["discovered_at"] = isoformat(self.discovered_at)
        data["last_checked_at"] = isoformat(self.last_checked_at)
        data["website_checked_at"] = isoformat(self.website_checked_at)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lead":
        payload = dict(data or {})
        payload["website_status"] = WebsiteStatus(
            payload.get("website_status") or WebsiteStatus.NOT_CHECKED
        )
        payload["website_quality"] = WebsiteQuality(
            payload.get("website_quality") or WebsiteQuality.UNKNOWN
        )
        payload["business_status"] = BusinessStatus(
            payload.get("business_status") or BusinessStatus.UNKNOWN
        )
        payload["score_confidence"] = Confidence(
            payload.get("score_confidence") or Confidence.LOW
        )
        payload["priority"] = LeadPriority(payload.get("priority") or LeadPriority.COLD)
        for key in ("discovered_at", "last_checked_at", "website_checked_at"):
            payload[key] = parse_datetime(payload.get(key))
        payload["social_links"] = dict(payload.get("social_links") or {})
        payload["raw"] = dict(payload.get("raw") or {})
        payload["score_reason"] = list(payload.get("score_reason") or [])
        payload["score_breakdown"] = {
            k: int(v) for k, v in (payload.get("score_breakdown") or {}).items()
        }
        payload["categories"] = list(payload.get("categories") or [])
        payload.setdefault("business_name", "")
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})

    @property
    def has_website(self) -> bool:
        return self.website_status == WebsiteStatus.EXISTS

    def confidence_rank(self) -> int:
        """Numeric rank for confidence, so leads can be sorted by it."""
        return {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}.get(
            self.score_confidence, 0
        )

    def summary_row(self) -> List[str]:
        """Row used by the CLI table printer."""
        return [
            self.business_name or "(unknown)",
            self.city or "-",
            self.website_url or "-",
            str(self.website_status),
            str(self.lead_score),
            str(self.score_confidence),
            str(self.priority),
        ]


def dedupe_leads(leads: Iterable[Lead]) -> List[Lead]:
    """Merge duplicate leads, keeping the richest record.

    Two leads are duplicates when they share a ``dedupe_key``. When merging we
    keep the record that carries more information (more non-empty fields) and
    fill any gaps from the other record.
    """
    best: Dict[str, Lead] = {}
    for lead in leads:
        key = lead.dedupe_key or lead.compute_dedupe_key()
        existing = best.get(key)
        if existing is None:
            best[key] = lead
            continue
        best[key] = merge_leads(existing, lead)
    return list(best.values())


def _filled_fields(lead: Lead) -> int:
    count = 0
    for value in lead.to_dict().values():
        if value in (None, "", [], {}, 0):
            continue
        count += 1
    return count


def merge_leads(primary: Lead, secondary: Lead) -> Lead:
    """Merge two records for the same business into one richer lead."""
    if _filled_fields(secondary) > _filled_fields(primary):
        primary, secondary = secondary, primary

    for name in primary.__dataclass_fields__:
        if name in {"raw", "social_links", "categories", "score_reason", "score_breakdown"}:
            continue
        current = getattr(primary, name)
        other = getattr(secondary, name)
        if current in (None, "", 0) and other not in (None, "", 0):
            setattr(primary, name, other)

    primary.raw = {**secondary.raw, **primary.raw}
    primary.social_links = {**secondary.social_links, **primary.social_links}
    primary.categories = sorted(set(primary.categories) | set(secondary.categories))
    primary.last_checked_at = primary.last_checked_at or secondary.last_checked_at
    return primary


__all__ = [
    "Lead",
    "LeadScore",
    "WebsiteCheckResult",
    "dedupe_leads",
    "merge_leads",
    "utcnow",
    "isoformat",
    "parse_datetime",
    "normalize_name",
    "normalize_phone",
    "normalize_url",
]