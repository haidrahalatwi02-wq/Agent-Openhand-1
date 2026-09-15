"""Turn messy provider records into clean, consistent :class:`Lead` objects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from lead_finder_agent.models import (
    BusinessStatus,
    Lead,
    normalize_url,
)
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import extract_domain, normalize_whitespace, truncate

log = get_logger("extraction.normalizer")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Fields that must never appear in a Lead. This is a guardrail, not a parser:
# if a provider tries to hand us personal data we drop it.
_FORBIDDEN_FIELDS = (
    "national_id",
    "passport",
    "birthdate",
    "birthday",
    "personal_email",
    "home_address",
    "credit_card",
    "ssn",
    "password",
)


@dataclass
class LeadNormalizer:
    """Normalizes raw provider records into :class:`Lead` instances."""

    default_country: Optional[str] = None
    default_city: Optional[str] = None
    max_description: int = 500

    def normalize(self, raw: Mapping[str, Any], source: Optional[str] = None) -> Optional[Lead]:
        """Clean one raw record. Returns ``None`` when it is unusable."""
        if not isinstance(raw, Mapping):
            log.debug("Skipping non-mapping record: %r", raw)
            return None

        data = {k: v for k, v in raw.items() if k not in _FORBIDDEN_FIELDS}

        name = normalize_whitespace(
            data.get("business_name") or data.get("name") or data.get("title")
        )
        if not name:
            log.debug("Skipping record without a business name: %r", raw)
            return None

        social_links = self._clean_social(data.get("social_links"))
        website = self._pick_website(data.get("website_url"), social_links)

        lead = Lead(
            business_name=name,
            business_type=normalize_whitespace(
                data.get("business_type") or data.get("type") or data.get("category")
            ),
            country=normalize_whitespace(data.get("country")) or self.default_country,
            city=normalize_whitespace(data.get("city")) or self.default_city,
            address=normalize_whitespace(data.get("address")),
            phone=self.clean_phone(data.get("phone")),
            email=self.clean_email(data.get("email")),
            source=source or normalize_whitespace(data.get("source")) or "unknown",
            source_url=normalize_url(data.get("source_url")),
            website_url=website,
            social_links=social_links,
            description=truncate(data.get("description"), self.max_description),
            latitude=self._to_float(data.get("latitude") or data.get("lat")),
            longitude=self._to_float(data.get("longitude") or data.get("lon") or data.get("lng")),
            categories=self._clean_categories(data.get("categories")),
            review_count=self._to_int(data.get("review_count") or data.get("reviews")),
            rating=self._to_float(data.get("rating")),
            business_status=self._to_status(data.get("business_status")),
            raw=self._clean_raw(data),
        )
        return lead

    def normalize_many(
        self, raws: Iterable[Mapping[str, Any]], source: Optional[str] = None
    ) -> List[Lead]:
        leads: List[Lead] = []
        for raw in raws:
            lead = self.normalize(raw, source=source)
            if lead is not None:
                leads.append(lead)
        return leads

    # -- field helpers -----------------------------------------------------

    @staticmethod
    def clean_phone(value: Any) -> Optional[str]:
        """Keep a human-readable but tidy phone number."""
        text = normalize_whitespace(value)
        if not text:
            return None
        # Keep leading +, digits, spaces, dashes and parentheses.
        cleaned = re.sub(r"[^\d+\-\s()]", "", str(text)).strip()
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) < 5:
            return None
        return cleaned or None

    @staticmethod
    def clean_email(value: Any) -> Optional[str]:
        text = normalize_whitespace(value)
        if not text:
            return None
        match = _EMAIL_RE.search(str(text))
        return match.group(0).lower() if match else None

    @staticmethod
    def _clean_social(value: Any) -> Dict[str, str]:
        if not value:
            return {}
        result: Dict[str, str] = {}
        if isinstance(value, Mapping):
            for key, url in value.items():
                cleaned = normalize_url(url)
                if cleaned:
                    result[str(key)] = cleaned
            return result
        if isinstance(value, (list, tuple, set)):
            for url in value:
                cleaned = normalize_url(url)
                domain = extract_domain(cleaned) if cleaned else None
                if cleaned and domain:
                    result[domain.split(".")[0]] = cleaned
        return result

    @staticmethod
    def _pick_website(website: Any, social_links: Mapping[str, str]) -> Optional[str]:
        """Choose the website URL, ignoring values that are just a social link."""
        cleaned = normalize_url(website)
        if cleaned:
            return cleaned
        # Providers sometimes stuff a Facebook URL into the website field; that
        # is a social presence, not a website, so it stays in social_links only.
        return None

    @staticmethod
    def _clean_categories(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            parts = [str(p).strip() for p in value]
        else:
            return []
        seen = []
        for part in parts:
            if part and part not in seen:
                seen.append(part)
        return seen

    @staticmethod
    def _clean_raw(data: Mapping[str, Any]) -> Dict[str, Any]:
        """Store the raw record, JSON-safe and bounded in size."""
        clean: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key] = value
            elif isinstance(value, (list, tuple)):
                clean[key] = [v for v in value if isinstance(v, (str, int, float, bool))]
            elif isinstance(value, Mapping):
                clean[key] = {
                    k: v for k, v in value.items() if isinstance(v, (str, int, float, bool))
                }
            else:
                clean[key] = str(value)
        return clean

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_status(value: Any) -> BusinessStatus:
        """Map a provider status hint onto our enum, defaulting to unknown."""
        if value is None or value == "":
            return BusinessStatus.UNKNOWN
        if isinstance(value, BusinessStatus):
            return value
        text = str(value).strip().lower()
        if text in {"active", "open", "operational", "operating", "yes", "true"}:
            return BusinessStatus.ACTIVE
        if text in {"closed", "permanently_closed", "shut", "no", "false"}:
            return BusinessStatus.CLOSED
        return BusinessStatus.UNKNOWN


def normalize_lead(
    raw: Mapping[str, Any],
    source: Optional[str] = None,
    normalizer: Optional[LeadNormalizer] = None,
) -> Optional[Lead]:
    """Convenience wrapper around :meth:`LeadNormalizer.normalize`."""
    return (normalizer or LeadNormalizer()).normalize(raw, source=source)


def normalize_leads(
    raws: Iterable[Mapping[str, Any]],
    source: Optional[str] = None,
    normalizer: Optional[LeadNormalizer] = None,
) -> List[Lead]:
    """Convenience wrapper around :meth:`LeadNormalizer.normalize_many`."""
    return (normalizer or LeadNormalizer()).normalize_many(raws, source=source)


__all__ = ["LeadNormalizer", "normalize_lead", "normalize_leads"]
