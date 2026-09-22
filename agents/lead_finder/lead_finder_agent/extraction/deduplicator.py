"""De-duplication of leads across providers.

Two layers of matching are applied, in this order:

1. **Provider identity** - a ``dedupe_key`` built from ``source:source_id``.
   Only records from the *same* provider can collide here, because ids are
   namespaced by provider and are never compared across providers.
2. **Cross-provider matching** - a conservative, multi-signal comparison that
   recognises the same business listed by two different providers.

Cross-provider matching is deliberately hard to satisfy. Merging two records
that are actually different businesses loses a real lead, and that is worse
than showing the same business twice, so a name match alone is never enough:
the name must be very similar *and* be corroborated by an independent signal
(a shared phone, an almost identical address, effectively the same
coordinates, or the same website domain). Where the signals actively
contradict each other - two different phone numbers, two different domains -
the records are kept apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from lead_finder_agent.models import Lead, dedupe_leads, merge_leads
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import (
    comparable_address,
    comparable_name,
    coordinates_close,
    name_similarity,
    same_domain,
    same_phone,
)

log = get_logger("extraction.deduplicator")


@dataclass
class Deduplicator:
    """Collapses duplicate leads.

    Parameters
    ----------
    fuzzy:
        Enable cross-provider matching on top of exact key matching.
    similarity_threshold:
        Minimum name similarity (0..1) for a cross-provider match. Names equal
        after normalization always score 1.0, so this governs how much
        variation is tolerated.
    address_threshold:
        Minimum address similarity (0..1) for an address to corroborate a name
        match on its own.
    location_conflict_tolerance:
        Records whose coordinates are further apart than this (in degrees,
        ~111 km each) are treated as different businesses, whatever the names.
    """

    fuzzy: bool = True
    similarity_threshold: float = 0.90
    weak_similarity_threshold: float = 0.60
    address_threshold: float = 0.90
    location_conflict_tolerance: float = 0.05
    _merged_count: int = 0

    @property
    def merged_count(self) -> int:
        """How many duplicate records were collapsed in the last run."""
        return self._merged_count

    def deduplicate(self, leads: Iterable[Lead]) -> List[Lead]:
        """Return a new, de-duplicated list of leads.

        Ordering is deterministic: survivors keep the order in which they were
        first seen, and a merge never moves a record.
        """
        incoming = list(leads)
        exact = dedupe_leads(incoming)
        self._merged_count = len(incoming) - len(exact)
        if not self.fuzzy:
            return exact
        return self._fuzzy_pass(exact)

    # -- internals ---------------------------------------------------------

    def _fuzzy_pass(self, leads: List[Lead]) -> List[Lead]:
        result: List[Lead] = []
        # Bucket by a few leading characters so a large result set does not
        # become an O(n^2) comparison. The bucket is a cheap pre-filter only:
        # every candidate is still checked by ``_is_same_business``.
        buckets: Dict[str, List[int]] = {}

        for lead in leads:
            match_index: Optional[int] = None
            for key in self._bucket_keys(lead):
                for candidate_index in buckets.get(key, ()):
                    if self._is_same_business(lead, result[candidate_index]):
                        match_index = candidate_index
                        break
                if match_index is not None:
                    break

            if match_index is None:
                position = len(result)
                result.append(lead)
                for key in self._bucket_keys(lead):
                    buckets.setdefault(key, []).append(position)
            else:
                result[match_index] = merge_leads(result[match_index], lead)
                self._merged_count += 1

        if self._merged_count:
            log.debug("De-duplication merged %s record(s)", self._merged_count)
        return result

    @staticmethod
    def _bucket_keys(lead: Lead) -> Tuple[str, ...]:
        """Cheap candidate keys for ``lead``.

        The normalized prefix is the precise bucket; the bare first word is a
        wider one, so a record whose name gained a leading word is still
        compared instead of being silently skipped.
        """
        name = comparable_name(lead.business_name)
        letters = "".join(ch for ch in name if ch.isalnum())
        keys = []
        if letters:
            keys.append(letters[:3])
            first_word = "".join(ch for ch in name.split(" ")[0] if ch.isalnum())
            if first_word and first_word[:3] not in keys:
                keys.append(first_word[:3])
        return tuple(keys)

    def _is_same_business(self, first: Lead, second: Lead) -> bool:
        """Whether two records describe the same business.

        Conservative by construction. Conflicting contact details are checked
        first and veto the match outright; a name match alone is never enough.
        """
        name_a = comparable_name(first.business_name)
        name_b = comparable_name(second.business_name)
        if not name_a or not name_b:
            return False

        # Contact details that disagree mean two different businesses. This is
        # what stops two same-named branches, or two unrelated businesses that
        # happen to share a website, from collapsing into one.
        phone_conflict = self._conflicting(first.phone, second.phone, same_phone)
        domain_conflict = self._conflicting(first.website_url, second.website_url, same_domain)
        if phone_conflict or domain_conflict:
            return False

        phone_match = same_phone(first.phone, second.phone)
        domain_match = same_domain(first.website_url, second.website_url)
        ratio = name_similarity(first.business_name, second.business_name)

        # Tier 1: the names are close enough to plausibly be one name written
        # two ways, so any independent agreement confirms it.
        if ratio >= self.similarity_threshold:
            if (
                phone_match
                or domain_match
                or self._same_city(first, second)
                or self._same_address(first, second)
            ):
                return not self._location_conflict(first, second)
            # No city on either record, but the same name pinned to the same
            # point is still one business.
            return name_a == name_b and self._same_coordinates(first, second)

        # Tier 2: the names are only loosely similar (a trading name against a
        # legal one, say). This is where false positives live, so it takes a
        # shared phone or website - both of which cannot be coincidence - and
        # the names must not be plainly different.
        if ratio < self.weak_similarity_threshold:
            return False
        if not (phone_match or domain_match):
            return False
        return self._same_city(first, second) and not self._location_conflict(first, second)

    @staticmethod
    def _conflicting(first: Optional[str], second: Optional[str], comparator) -> bool:
        """True when both sides have a value that the comparator says differs."""
        if not first or not second:
            return False
        return not comparator(first, second)

    def _same_city(self, first: Lead, second: Lead) -> bool:
        city_a = comparable_address(first.city)
        city_b = comparable_address(second.city)
        if not city_a or not city_b:
            return False
        if city_a == city_b:
            return True
        return name_similarity(first.city, second.city) >= 0.85

    def _same_address(self, first: Lead, second: Lead) -> bool:
        address_a = comparable_address(first.address)
        address_b = comparable_address(second.address)
        if not address_a or not address_b:
            return False
        if address_a == address_b:
            return True
        return name_similarity(first.address, second.address) >= self.address_threshold

    def _same_coordinates(self, first: Lead, second: Lead) -> bool:
        return coordinates_close(
            first.latitude, first.longitude, second.latitude, second.longitude
        )

    def _location_conflict(self, first: Lead, second: Lead) -> bool:
        """Whether two records are far enough apart to be different businesses.

        Only consulted when both sides have coordinates; a missing position is
        unknown, not contradictory.
        """
        if None in (first.latitude, first.longitude, second.latitude, second.longitude):
            return False
        return not coordinates_close(
            first.latitude,
            first.longitude,
            second.latitude,
            second.longitude,
            tolerance=self.location_conflict_tolerance,
        )


def deduplicate(leads: Iterable[Lead], fuzzy: bool = True) -> List[Lead]:
    """Module level convenience wrapper."""
    return Deduplicator(fuzzy=fuzzy).deduplicate(leads)


__all__ = ["Deduplicator", "deduplicate"]
