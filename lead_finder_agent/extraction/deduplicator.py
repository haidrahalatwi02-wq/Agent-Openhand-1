"""De-duplication of leads.

Two layers of matching are applied:

1. **Exact key** - the ``dedupe_key`` (provider id, or normalized name+city+phone).
2. **Fuzzy** - normalized name similarity plus an overlapping city or phone,
   which catches the same business listed by two different providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional

from lead_finder_agent.models import Lead, dedupe_leads, merge_leads
from lead_finder_agent.models.lead import normalize_name, normalize_phone
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("extraction.deduplicator")


@dataclass
class Deduplicator:
    """Collapses duplicate leads.

    Parameters
    ----------
    fuzzy:
        Enable name-similarity matching on top of exact key matching.
    similarity_threshold:
        Minimum ``SequenceMatcher`` ratio (0..1) to treat two names as the same
        business.
    """

    fuzzy: bool = True
    similarity_threshold: float = 0.90
    _merged_count: int = 0

    @property
    def merged_count(self) -> int:
        """How many duplicate records were collapsed in the last run."""
        return self._merged_count

    def deduplicate(self, leads: Iterable[Lead]) -> List[Lead]:
        """Return a new, de-duplicated list of leads."""
        incoming = list(leads)
        exact = dedupe_leads(incoming)
        self._merged_count = len(incoming) - len(exact)
        if not self.fuzzy:
            return exact
        return self._fuzzy_pass(exact)

    # -- internals ---------------------------------------------------------

    def _fuzzy_pass(self, leads: List[Lead]) -> List[Lead]:
        result: List[Lead] = []
        # Bucket by first letters of the name to avoid O(n^2) growth on large sets.
        buckets: Dict[str, List[int]] = {}

        for lead in leads:
            key = self._bucket_key(lead)
            match_index: Optional[int] = None
            for candidate_index in buckets.get(key, []):
                if self._is_same_business(lead, result[candidate_index]):
                    match_index = candidate_index
                    break

            if match_index is None:
                buckets.setdefault(key, []).append(len(result))
                result.append(lead)
            else:
                result[match_index] = merge_leads(result[match_index], lead)
                self._merged_count += 1

        if self._merged_count:
            log.debug("De-duplication merged %s record(s)", self._merged_count)
        return result

    @staticmethod
    def _comparable_name(value: str) -> str:
        """Name reduced to lowercase alphanumerics separated by single spaces."""
        name = normalize_name(value)
        letters = "".join(ch if ch.isalnum() else " " for ch in name)
        return " ".join(letters.split())

    @staticmethod
    def _bucket_key(lead: Lead) -> str:
        """First few alphanumeric characters of the name, used for bucketing.

        Punctuation is stripped so ``"Al-Bahr"`` and ``"Al Bahr"`` land in the
        same bucket and can be compared by :meth:`_is_same_business`.
        """
        name = normalize_name(lead.business_name)
        letters = "".join(ch for ch in name if ch.isalnum())
        return letters[:3]

    def _is_same_business(self, first: Lead, second: Lead) -> bool:
        name_a = self._comparable_name(first.business_name)
        name_b = self._comparable_name(second.business_name)
        if not name_a or not name_b:
            return False

        # Identical phones are conclusive, wherever the business was listed.
        phone_a = normalize_phone(first.phone)
        phone_b = normalize_phone(second.phone)
        if phone_a and phone_b and phone_a == phone_b:
            return True

        ratio = SequenceMatcher(None, name_a, name_b).ratio()
        if ratio < self.similarity_threshold:
            return False

        # Same-ish name: require at least one corroborating location signal,
        # otherwise two unrelated "City Cafe" branches would collapse.
        if self._same_city(first, second):
            return True
        domain_a = normalize_name(str(first.website_url or ""))
        domain_b = normalize_name(str(second.website_url or ""))
        return bool(domain_a and domain_a == domain_b)

    @staticmethod
    def _same_city(first: Lead, second: Lead) -> bool:
        city_a = normalize_name(first.city)
        city_b = normalize_name(second.city)
        if not city_a or not city_b:
            return False
        if city_a == city_b:
            return True
        return SequenceMatcher(None, city_a, city_b).ratio() >= 0.85


def deduplicate(leads: Iterable[Lead], fuzzy: bool = True) -> List[Lead]:
    """Module level convenience wrapper."""
    return Deduplicator(fuzzy=fuzzy).deduplicate(leads)


__all__ = ["Deduplicator", "deduplicate"]
