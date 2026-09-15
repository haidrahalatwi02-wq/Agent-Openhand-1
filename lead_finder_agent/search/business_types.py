"""Resolver that maps business-type keywords to provider specific tags."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lead_finder_agent.config.loader import load_packaged_data
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import normalize_whitespace

log = get_logger("search.business_types")


def categories_match(left: Optional[str], right: Optional[str]) -> bool:
    """Whether two category values name the same vertical.

    Canonical categories are plural (``restaurants``) while provider records
    often carry the singular form (``restaurant``), so a plain string comparison
    would reject an otherwise valid match. Only a trailing plural ``s`` is
    folded; nothing else is guessed at.
    """
    first = (left or "").strip().lower()
    second = (right or "").strip().lower()
    if not first or not second:
        return False
    if first == second:
        return True
    return first.rstrip("s") == second.rstrip("s")


@dataclass
class BusinessTypeResolver:
    """Maps free text like ``"clothing shops"`` to OpenStreetMap selectors."""

    categories: Dict[str, Dict[str, object]] = field(default_factory=dict)
    fallback_tags: List[str] = field(default_factory=lambda: ["shop=*"])

    @classmethod
    def load(cls, path: Optional[object] = None) -> "BusinessTypeResolver":
        """Load category definitions, optionally from a custom rules file.

        ``path`` may be a :class:`~pathlib.Path` or string. When it does not
        exist the packaged defaults are used and a warning is logged, so a typo
        in configuration cannot break a search.
        """
        data = None
        if path:
            from pathlib import Path

            from lead_finder_agent.config.loader import load_config_file, merge_dicts

            candidate = Path(path)
            if candidate.is_file():
                data = merge_dicts(load_packaged_data("business_types"), load_config_file(candidate))
            else:
                log.warning("Business types file %s not found; using packaged defaults", candidate)
        if data is None:
            data = load_packaged_data("business_types")
        return cls(
            categories=dict(data.get("categories") or {}),
            fallback_tags=list(data.get("fallback_tags") or ["shop=*"]),
        )

    def resolve(self, business_type: Optional[str], keywords: Optional[List[str]] = None) -> List[str]:
        """Return OSM tag selectors for a business type and/or keywords.

        Matching is a lowercase substring test against each category's aliases,
        so ``"restaurant"`` and ``"مطاعم"`` both resolve to food venues. When
        several categories match, the one with the *longest* matching alias
        wins: ``"rocket shop"`` must resolve to the specific category rather
        than to a generic ``shop`` alias.
        """
        haystack = " ".join(
            filter(None, [normalize_whitespace(business_type) or ""] + list(keywords or []))
        ).lower()

        if not haystack.strip():
            return list(self.fallback_tags)

        best: Optional[tuple[int, str, List[str]]] = None
        for name, spec in self.categories.items():
            tags = spec.get("tags") or []
            if not tags:
                continue
            for alias in spec.get("aliases") or []:
                alias_text = str(alias).lower()
                if alias_text and alias_text in haystack:
                    score = len(alias_text)
                    if best is None or score > best[0]:
                        best = (score, name, [str(tag) for tag in tags])

        if best is not None:
            log.debug("Business type %r resolved to category %r", business_type, best[1])
            return list(best[2])

        # No category matched: fall back so a search still returns something
        # useful instead of nothing.
        log.debug("No category matched %r; using fallback tags", business_type)
        return list(self.fallback_tags)

    def category_for(self, business_type: Optional[str], keywords: Optional[List[str]] = None) -> Optional[str]:
        """Resolve to a category name, or ``None`` when nothing matches."""
        tags = self.resolve(business_type, keywords)
        if tags == list(self.fallback_tags):
            return None
        for name, spec in self.categories.items():
            if [str(tag) for tag in (spec.get("tags") or [])] == tags:
                return name
        return None


__all__ = ["BusinessTypeResolver"]
