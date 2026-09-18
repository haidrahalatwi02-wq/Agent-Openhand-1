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
    WebsiteErrorKind,
    WebsiteIdentity,
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
    # --- verification detail (additive; older records deserialize unchanged) ---
    #: URL that was actually reached, after any redirects.
    final_url: Optional[str] = None
    #: Wall-clock duration of the whole check, in milliseconds.
    response_time_ms: Optional[int] = None
    #: Structured failure reason; see :class:`WebsiteErrorKind`.
    error_kind: Optional[WebsiteErrorKind] = None
    #: ``<title>`` text when the response body could be parsed safely.
    page_title: Optional[str] = None
    #: Where the candidate URL came from, for provenance.
    website_source: Optional[str] = None
    #: How strongly the page could be tied to the business.
    identity: WebsiteIdentity = WebsiteIdentity.NOT_APPLICABLE
    #: True when the body hit the configured size cap and was cut short.
    truncated: bool = False
    #: Set when the result came from the per-run cache instead of a new request.
    from_cache: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["quality"] = str(self.quality)
        data["error_kind"] = str(self.error_kind) if self.error_kind else None
        data["identity"] = str(self.identity)
        data["checked_at"] = isoformat(self.checked_at)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebsiteCheckResult":
        payload = dict(data or {})
        payload["status"] = WebsiteStatus(payload.get("status") or WebsiteStatus.NOT_CHECKED)
        payload["quality"] = WebsiteQuality(payload.get("quality") or WebsiteQuality.UNKNOWN)
        error_kind = payload.get("error_kind")
        payload["error_kind"] = WebsiteErrorKind(error_kind) if error_kind else None
        identity = payload.get("identity")
        payload["identity"] = (
            WebsiteIdentity(identity) if identity else WebsiteIdentity.NOT_APPLICABLE
        )
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
    #: Version of the scoring rule set that produced this score.
    scoring_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "confidence": str(self.confidence),
            "reasons": list(self.reasons),
            "priority": str(self.priority),
            "breakdown": dict(self.breakdown),
            "scoring_version": self.scoring_version,
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
            scoring_version=int(payload.get("scoring_version") or 1),
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
    scoring_version: int = 1
    priority: LeadPriority = LeadPriority.COLD
    discovered_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    dedupe_key: Optional[str] = None
    #: Every provider that reported this business. A merged record keeps the
    #: whole list, so a lead discovered by both Google and OSM can say so
    #: instead of silently attributing itself to whichever ran first.
    sources: List[str] = field(default_factory=list)
    #: Provider name -> that provider's own stable id, e.g.
    #: ``{"google_places": "ChIJ...", "osm": "node/456"}``. Ids are never
    #: compared across providers: an OSM node id and a Google place id are
    #: unrelated namespaces that could otherwise collide by accident.
    provider_ids: Dict[str, str] = field(default_factory=dict)
    #: Provider name -> that provider's public record URL.
    source_urls: Dict[str, str] = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------

    def __post_init__(self) -> None:
        self.business_name = (self.business_name or "").strip()
        self.website_status = WebsiteStatus(self.website_status)
        self.website_quality = WebsiteQuality(self.website_quality)
        self.business_status = BusinessStatus(self.business_status)
        self.score_confidence = Confidence(self.score_confidence)
        self.priority = LeadPriority(self.priority)
        self.discovered_at = self.discovered_at or utcnow()
        self._sync_provenance()
        if not self.dedupe_key:
            self.dedupe_key = self.compute_dedupe_key()
        if not self.id:
            self.id = self.dedupe_key

    def _sync_provenance(self) -> None:
        """Keep ``sources`` and the per-provider maps consistent with the record."""
        self.sources = [s for s in (self.sources or []) if s]
        if self.source and self.source not in self.sources:
            self.sources.insert(0, self.source)
        if not self.source and self.sources:
            self.source = self.sources[0]

        self.provider_ids = {k: str(v) for k, v in (self.provider_ids or {}).items() if v}
        self.source_urls = {k: v for k, v in (self.source_urls or {}).items() if v}

        source_id = str(self.raw.get("source_id") or "").strip()
        if self.source and source_id:
            # ``setdefault``: a value already recorded from an earlier merge is
            # never replaced by this record's own id.
            self.provider_ids.setdefault(self.source, source_id)
        if self.source and self.source_url:
            self.source_urls.setdefault(self.source, self.source_url)

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
        self.scoring_version = result.scoring_version
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
        payload["sources"] = [str(s) for s in (payload.get("sources") or []) if s]
        payload["provider_ids"] = {
            str(k): str(v) for k, v in (payload.get("provider_ids") or {}).items() if v
        }
        payload["source_urls"] = {
            str(k): str(v) for k, v in (payload.get("source_urls") or {}).items() if v
        }
        payload["score_reason"] = list(payload.get("score_reason") or [])
        payload["score_breakdown"] = {
            k: int(v) for k, v in (payload.get("score_breakdown") or {}).items()
        }
        try:
            payload["scoring_version"] = int(payload.get("scoring_version") or 1)
        except (TypeError, ValueError):
            payload["scoring_version"] = 1
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

    def sort_key(self) -> tuple:
        """Total ordering key for ranking leads.

        Score first, then confidence, then name and finally the dedupe key. The
        trailing fields matter: without them two leads sharing a score would fall
        back to input order, so the same data could rank differently between
        runs. Names are lower-cased before comparison so ordering is stable
        regardless of capitalisation or surrounding whitespace.

        Returns a key suitable for ``sorted(..., reverse=True)``.
        """
        name = (self.business_name or "").strip().lower()
        return (
            int(self.lead_score or 0),
            self.confidence_rank(),
            name,
            (self.business_name or "").strip(),
            self.dedupe_key or self.id or "",
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
    """Merge two records for the same business into one richer lead.

    The rule is *fill gaps, never overwrite*: a field that already holds a value
    is kept, and only an empty one is filled from the other record. Two
    providers that disagree therefore keep the first non-empty answer rather
    than one silently clobbering the other, and a merge can never lose data.

    Provenance is additive: both providers stay in ``sources`` and both keep
    their own ids, so the merged lead can still be traced back to every source
    that reported it.
    """
    if _filled_fields(secondary) > _filled_fields(primary):
        primary, secondary = secondary, primary

    for name in primary.__dataclass_fields__:
        if name in {
            "raw",
            "social_links",
            "categories",
            "score_reason",
            "score_breakdown",
            "sources",
            "provider_ids",
            "source_urls",
        }:
            continue
        current = getattr(primary, name)
        other = getattr(secondary, name)
        if current in (None, "", 0) and other not in (None, "", 0):
            setattr(primary, name, other)

    primary.raw = {**secondary.raw, **primary.raw}
    primary.social_links = {**secondary.social_links, **primary.social_links}
    primary.categories = sorted(set(primary.categories) | set(secondary.categories))
    primary.last_checked_at = primary.last_checked_at or secondary.last_checked_at

    # Provenance: union, order-stable, primary first.
    merged_sources = list(primary.sources)
    for source in secondary.sources or ([secondary.source] if secondary.source else []):
        if source and source not in merged_sources:
            merged_sources.append(source)
    if primary.source and primary.source not in merged_sources:
        merged_sources.insert(0, primary.source)
    primary.sources = merged_sources

    # Ids stay per provider: a secondary id never displaces the primary's own.
    for provider, provider_id in (secondary.provider_ids or {}).items():
        primary.provider_ids.setdefault(provider, provider_id)
    for provider, url in (secondary.source_urls or {}).items():
        primary.source_urls.setdefault(provider, url)
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