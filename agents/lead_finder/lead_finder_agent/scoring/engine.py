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
    WebsiteCheckResult,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.scoring.base import BaseLeadScorer
from lead_finder_agent.scoring.rules import ScoringRules, load_scoring_rules
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("scoring.engine")

# Which signals count toward "how much do we actually know?" confidence.
#
# NOTE: ``website_status_known`` is true only for a *conclusive* check (the
# business has a site, or a server confirmed it has none). An inconclusive or
# never-run check adds no confidence: "we did not check" is not knowledge.
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


def hydrate_website_check(lead: Lead) -> Optional[WebsiteCheckResult]:
    """Recover the stored check result from ``lead.raw``, if it is usable.

    Scoring must be able to run against a stored lead without re-running the
    checker. Anything that cannot be parsed is discarded, so corrupt data can
    never be mistaken for a conclusive "this business has no website".
    """
    raw = getattr(lead, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    payload = raw.get("website_check")
    if not isinstance(payload, Mapping) or not payload:
        return None
    try:
        return WebsiteCheckResult.from_dict(dict(payload))
    except (ValueError, TypeError) as exc:
        log.debug("Ignoring malformed stored website_check for %r: %s", lead.business_name, exc)
        return None


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
        stored = hydrate_website_check(lead)
        if stored is not None:
            social_only = bool(stored.social_only)

    has_website = status == WebsiteStatus.EXISTS
    website_is_good = has_website and quality == WebsiteQuality.GOOD
    website_is_weak = has_website and quality == WebsiteQuality.WEAK
    if social_only:
        website_is_weak = False  # reported through website_social_only instead

    # The website statuses are deliberately kept distinct. A provider omitting a
    # website field (``website_null``) is *absence of evidence*; only a completed
    # check returning ``website_not_found`` is evidence of absence.
    website_missing = status == WebsiteStatus.NOT_FOUND
    website_unknown = status == WebsiteStatus.UNKNOWN
    website_unreachable = status == WebsiteStatus.UNREACHABLE
    website_null = (
        not has_website
        and not website_missing
        and status == WebsiteStatus.NOT_CHECKED
        and not lead.website_url
    )
    website_status_known = status in (WebsiteStatus.EXISTS, WebsiteStatus.NOT_FOUND)

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
        # --- website opportunity -------------------------------------------
        "website_missing": website_missing,
        "website_unknown": website_unknown,
        "website_unreachable": website_unreachable,
        "website_null": website_null,
        "website_status_known": website_status_known,
        # --- website condition (verified checker output only) ---------------
        "has_website": has_website,
        "website_reachable": reachable,
        "website_is_good": website_is_good,
        "website_is_weak": website_is_weak,
        "website_social_only": social_only or quality == WebsiteQuality.SOCIAL_ONLY,
        # --- business relevance ---------------------------------------------
        "has_business_name": _has(lead.business_name),
        "has_business_type": _has(lead.business_type),
        "business_active": lead.business_status == BusinessStatus.ACTIVE,
        "business_closed": lead.business_status == BusinessStatus.CLOSED,
        "business_active_or_closed": lead.business_status != BusinessStatus.UNKNOWN,
        # --- contactability / public data completeness -----------------------
        "has_phone": _has(lead.phone),
        "has_email": _has(lead.email),
        "has_address": _has(lead.address),
        "has_location": _has(lead.city) or _has(lead.country),
        "has_social": bool(lead.social_links),
        "has_description": _has(lead.description),
        "has_categories": bool(lead.categories),
        "has_source": _has(lead.source) or _has(lead.source_url),
        # --- reputation -------------------------------------------------------
        "has_reviews": bool(review_count and review_count > reviews_present),
        "has_good_rating": bool(rating and rating >= good_rating),
        "has_many_reviews": bool(review_count and review_count >= many_reviews),
        # --- data quality -----------------------------------------------------
        "data_quality_high": filled >= min_fields + 2,
        "data_quality_low": filled <= 2,
        "filled_field_count": filled,
    }
    return signals


@dataclass
class LeadScorer(BaseLeadScorer):
    """The default rule-based scorer."""

    rules: ScoringRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = load_scoring_rules()

    # -- public API --------------------------------------------------------

    def score(self, lead: Lead, check: Optional[Any] = None) -> LeadScore:
        """Compute a :class:`LeadScore` for ``lead`` without modifying it."""
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
        priority = self._priority(total, confidence, signals)

        if not reasons:
            reasons.append("No significant scoring signals found")

        return LeadScore(
            score=total,
            confidence=confidence,
            reasons=reasons,
            priority=priority,
            breakdown=breakdown,
            scoring_version=self.rules.version,
        )

    def score_lead(self, lead: Lead, check: Optional[Any] = None) -> Lead:
        """Score ``lead`` in place and return it."""
        return lead.apply_score(self.score(lead, check=check))

    def score_all(self, leads: Iterable[Lead], checks: Optional[Mapping[str, Any]] = None) -> List[Lead]:
        checks = checks or {}
        result = []
        for lead in leads:
            check = None
            for key in (lead.dedupe_key, lead.id):
                if key and key in checks:
                    check = checks[key]
                    break
            result.append(self.score_lead(lead, check=check))
        return result

    # -- internals ---------------------------------------------------------

    def _confidence(self, signals: Mapping[str, Any]) -> Confidence:
        """How much is actually known about this lead.

        This describes confidence in the *score*, never certainty that the
        business has no website.
        """
        known = 0
        for key in _CONFIDENCE_SIGNALS:
            if signals.get(key) is True:
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

    def _is_prime_prospect(self, signals: Mapping[str, Any]) -> bool:
        """True when the data actually established that the business needs a site.

        "Hot" means prime prospect for a *new* website, so it requires evidence
        of a website gap: a server-confirmed missing site, a weak or parked one,
        or a social-only presence. An unverified check ("we could not tell") and
        a good working website are both excluded, so a data-rich record can never
        be promoted to hot on signals that never established a gap.
        """
        if signals.get("website_missing") or signals.get("website_is_weak"):
            return True
        return bool(signals.get("has_website") and signals.get("website_social_only"))

    def _priority(
        self, score: int, confidence: Confidence, signals: Mapping[str, Any]
    ) -> LeadPriority:
        # A closed business is never a viable prospect. Deciding this as a rule
        # (rather than relying on the penalty being large enough) keeps a rich
        # record from ever lifting a dead business into "warm".
        if signals.get("business_closed"):
            return LeadPriority.DISQUALIFIED

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
        # ...nor on a record that never established a website gap.
        if candidate == LeadPriority.HOT and not self._is_prime_prospect(signals):
            return LeadPriority.WARM
        return candidate


__all__ = ["LeadScorer", "build_signals", "hydrate_website_check"]
