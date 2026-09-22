"""Scoring pipeline: signals -> score, confidence, priority, reasons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from lead_finder_agent.models import (
    BusinessStatus,
    Confidence,
    Lead,
    LeadPriority,
    LeadScore,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.scoring.rules import ScoringRules, load_scoring_rules
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("scoring.engine")

# Which signals count toward "how much do we actually know?" confidence.
_CONFIDENCE_SIGNALS = (
    "has_phone",
    "has_email",
    "has_address",
    "has_social",
    "has_description",
    "has_categories",
    "business_active_or_closed",
    "website_status_known",
    "has_reviews",
)


def _has(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def build_signals(
    lead: Lead,
    check: Optional[Any] = None,
    rules: Optional[ScoringRules] = None,
) -> Dict[str, Any]:
    """Derive the boolean signal map used by scoring rules.

    ``check`` is an optional :class:`WebsiteCheckResult`. When omitted the lead's
    own website fields are used, which lets scoring run independently of the
    checker (useful in tests and when re-scoring stored leads).
    """
    rules = rules or load_scoring_rules()
    thresholds = rules.thresholds or {}

    status = lead.website_status
    quality = lead.website_quality
    reachable = False
    social_only = False

    if check is not None:
        status = getattr(check, "status", status)
        quality = getattr(check, "quality", quality)
        reachable = bool(getattr(check, "is_reachable", False))
        social_only = bool(getattr(check, "social_only", False))
    else:
        reachable = bool(lead.website_url) and status == WebsiteStatus.EXISTS
        raw_check = lead.raw.get("website_check") or {}
        social_only = bool(raw_check.get("social_only", False))

    has_website = status == WebsiteStatus.EXISTS
    website_is_good = has_website and quality == WebsiteQuality.GOOD
    website_is_weak = has_website and quality in (WebsiteQuality.WEAK, WebsiteQuality.SOCIAL_ONLY)
    if social_only:
        website_is_weak = False  # reported through website_social_only instead

    review_count = lead.review_count
    rating = lead.rating
    good_rating = float(thresholds.get("good_rating", 4.0))
    reviews_present = int(thresholds.get("reviews_present", 3))
    many_reviews = int(thresholds.get("many_reviews", 25))

    core_fields = [
        lead.business_name,
        lead.business_type,
        lead.city,
        lead.address,
        lead.phone,
        lead.email,
        lead.website_url,
        lead.social_links,
        lead.description,
        lead.categories,
    ]
    filled = sum(1 for value in core_fields if _has(value))
    min_fields = int(thresholds.get("data_quality_min_fields", 4))

    signals: Dict[str, Any] = {
        # website
        "has_website": has_website,
        "website_reachable": reachable,
        "website_is_good": website_is_good,
        "website_is_weak": website_is_weak or quality == WebsiteQuality.WEAK,
        "website_social_only": social_only or quality == WebsiteQuality.SOCIAL_ONLY,
        "website_null": not has_website and status == WebsiteStatus.NOT_CHECKED and not lead.website_url,
        "website_missing": status == WebsiteStatus.NOT_FOUND,
        "website_unknown": status in (WebsiteStatus.UNKNOWN, WebsiteStatus.UNREACHABLE),
        "website_status_known": status
        not in (WebsiteStatus.UNKNOWN, WebsiteStatus.UNREACHABLE, WebsiteStatus.NOT_CHECKED),
        # contactability
        "has_phone": _has(lead.phone),
        "has_email": _has(lead.email),
        "has_address": _has(lead.address),
        "has_social": bool(lead.social_links),
        "has_description": _has(lead.description),
        "has_categories": bool(lead.categories),
        # business reality
        "business_active": lead.business_status == BusinessStatus.ACTIVE,
        "business_closed": lead.business_status == BusinessStatus.CLOSED,
        "business_active_or_closed": lead.business_status != BusinessStatus.UNKNOWN,
        # reputation
        "has_reviews": bool(review_count and review_count > reviews_present),
        "has_good_rating": bool(rating and rating >= good_rating),
        "has_many_reviews": bool(review_count and review_count >= many_reviews),
        # data quality
        "data_quality_high": filled >= min_fields + 2,
        "data_quality_low": filled <= 2,
        "filled_field_count": filled,
    }
    return signals


@dataclass
class LeadScorer:
    """Applies :class:`ScoringRules` to leads."""

    rules: ScoringRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = load_scoring_rules()

    # -- public API --------------------------------------------------------

    def score(self, lead: Lead, check: Optional[Any] = None) -> LeadScore:
        """Compute a :class:`LeadScore` for ``lead``."""
        signals = build_signals(lead, check=check, rules=self.rules)
        breakdown: Dict[str, int] = {}
        reasons: List[str] = []
        total = 0

        for rule in self.rules.rules:
            if not rule.matches(signals):
                continue
            total += rule.points
            breakdown[rule.id] = rule.points
            if rule.description:
                reasons.append(rule.description)

        low = int(self.rules.score_min)
        high = int(self.rules.score_max)
        total = max(low, min(high, total))

        confidence = self._confidence(signals)
        priority = self._priority(total, confidence)

        if not reasons:
            reasons.append("No significant scoring signals found")

        return LeadScore(
            score=total,
            confidence=confidence,
            reasons=reasons,
            priority=priority,
            breakdown=breakdown,
        )

    def score_lead(self, lead: Lead, check: Optional[Any] = None) -> Lead:
        """Score ``lead`` in place and return it."""
        return lead.apply_score(self.score(lead, check=check))

    def score_all(self, leads: Iterable[Lead], checks: Optional[Mapping[str, Any]] = None) -> List[Lead]:
        checks = checks or {}
        result = []
        for lead in leads:
            check = checks.get(lead.dedupe_key or lead.id or "")
            result.append(self.score_lead(lead, check=check))
        return result

    # -- internals ---------------------------------------------------------

    def _confidence(self, signals: Mapping[str, Any]) -> Confidence:
        known = 0
        for key in _CONFIDENCE_SIGNALS:
            if key in signals and signals[key] is True:
                known += 1
        # A record with a phone number is meaningfully actionable even if thin.
        if signals.get("has_phone"):
            known += 1

        high = int(self.rules.confidence_thresholds.get("high_min_signals", 7))
        medium = int(self.rules.confidence_thresholds.get("medium_min_signals", 4))
        if known >= high:
            return Confidence.HIGH
        if known >= medium:
            return Confidence.MEDIUM
        return Confidence.LOW

    def _priority(self, score: int, confidence: Confidence) -> LeadPriority:
        thresholds = self.rules.thresholds or {}
        hot = int(thresholds.get("hot_score", 70))
        warm = int(thresholds.get("warm_score", 45))
        cold = int(thresholds.get("cold_score", 20))
        minimum = str(thresholds.get("min_confidence_for_priority", "low")).lower()

        rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
        allowed = {
            "low": Confidence.LOW,
            "medium": Confidence.MEDIUM,
            "high": Confidence.HIGH,
        }.get(minimum, Confidence.LOW)

        candidate = LeadPriority.COLD
        if score >= hot:
            candidate = LeadPriority.HOT
        elif score >= warm:
            candidate = LeadPriority.WARM
        elif score < cold:
            candidate = LeadPriority.DISQUALIFIED

        # Never label a lead "hot" on flimsy data.
        if rank[confidence] < rank[allowed] and candidate in (LeadPriority.HOT, LeadPriority.WARM):
            return LeadPriority.COLD
        return candidate


__all__ = ["LeadScorer", "build_signals"]
