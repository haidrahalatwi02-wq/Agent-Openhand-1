"""City and country resolution.

Search input reaches us as free text, and the same place is spelled many ways:
``عدن`` and ``Aden``, ``Sana'a`` and ``صنعاء``. Downstream stages (providers,
deduplication, storage filters) all compare names as plain strings, so mixed
spellings silently produce empty result sets.

:class:`LocationResolver` folds known spellings onto one canonical name. The
guiding rule is the same honesty rule the rest of the project follows: an
unrecognized value is **never guessed at** — it is returned unchanged, so a
typo degrades to the previous behaviour instead of inventing a location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from lead_finder_agent.config.loader import load_packaged_data
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import normalize_whitespace

log = get_logger("config.locations")

#: Characters that carry no meaning when comparing alternative spellings.
_PUNCTUATION = str.maketrans({"'": "", "’": "", "`": "", "ʿ": "", "-": " ", "_": " "})


def _key(value: Optional[str]) -> str:
    """Comparison key for a place name: lowercase, unpunctuated, single-spaced."""
    text = normalize_whitespace(value)
    if not text:
        return ""
    lowered = str(text).lower().translate(_PUNCTUATION)
    return " ".join(lowered.split())


def _strip_qualifier(value: str) -> str:
    """Drop a leading ``city``/``مدينة`` so ``مدينة عدن`` matches ``عدن``."""
    tokens = value.split()
    if len(tokens) > 1 and tokens[0] in {"city", "مدينة"}:
        return " ".join(tokens[1:])
    return value


@dataclass
class LocationResolver:
    """Maps free-text city/country names onto canonical spellings."""

    cities: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    countries: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _city_index: Dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _country_index: Dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._city_index = self._build_index(self.cities)
        self._country_index = self._build_index(self.countries)

    @staticmethod
    def _build_index(entries: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        index: Dict[str, str] = {}
        for canonical, spec in (entries or {}).items():
            candidates = [canonical] + list((spec or {}).get("aliases") or [])
            for candidate in candidates:
                for variant in {_key(candidate), _strip_qualifier(_key(candidate))}:
                    if variant:
                        # Canonical names win over a colliding alias.
                        index.setdefault(variant, canonical)
        return index

    @classmethod
    def load(cls, override: Optional[Dict[str, Any]] = None) -> "LocationResolver":
        """Load the packaged location data, optionally merging an override.

        A malformed or missing data file must not break a search, so failures
        fall back to an empty (pass-through) resolver.
        """
        try:
            data = load_packaged_data("locations")
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            log.warning("Location data unavailable (%s); names will pass through unchanged", exc)
            return cls()
        if override:
            from lead_finder_agent.config.loader import merge_dicts

            data = merge_dicts(data, override)
        return cls(
            cities=dict(data.get("cities") or {}),
            countries=dict(data.get("countries") or {}),
        )

    # -- lookups -----------------------------------------------------------

    def resolve_city(self, city: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(canonical_city, country)`` for ``city``.

        Unknown cities come back unchanged with a ``None`` country, which lets
        the caller keep whatever country the user supplied.
        """
        cleaned = normalize_whitespace(city)
        if not cleaned:
            return None, None
        canonical = self._lookup(self._city_index, cleaned)
        if canonical is None:
            return cleaned, None
        spec = self.cities.get(canonical) or {}
        return canonical, spec.get("country")

    def resolve_country(self, country: Optional[str]) -> Optional[str]:
        """Return the canonical country name, or the input when unrecognized."""
        cleaned = normalize_whitespace(country)
        if not cleaned:
            return None
        return self._lookup(self._country_index, cleaned) or cleaned

    @staticmethod
    def _lookup(index: Dict[str, str], value: str) -> Optional[str]:
        key = _key(value)
        if not key:
            return None
        if key in index:
            return index[key]
        stripped = _strip_qualifier(key)
        return index.get(stripped)

    @property
    def known_cities(self) -> List[str]:
        return sorted(self.cities)

    @property
    def known_countries(self) -> List[str]:
        return sorted(self.countries)


#: Process-wide cache. Loading the JSON on every query would be wasteful, and a
#: failure is remembered so a broken data file is reported once, not per search.
_RESOLVER: Optional[LocationResolver] = None
_RESOLVER_LOADED = False


def get_location_resolver() -> Optional[LocationResolver]:
    """The shared resolver, or ``None`` when the data cannot be loaded.

    Callers treat ``None`` as "no resolution available" and pass names through
    unchanged; a missing data file must never make searching fail.
    """
    global _RESOLVER, _RESOLVER_LOADED
    if _RESOLVER_LOADED:
        return _RESOLVER
    _RESOLVER_LOADED = True
    try:
        _RESOLVER = LocationResolver.load()
    except Exception:  # noqa: BLE001 - resolution is an enhancement, never a blocker
        log.warning("Location resolution disabled; names will pass through unchanged")
        _RESOLVER = None
    return _RESOLVER


def reset_location_resolver() -> None:
    """Drop the cached resolver so the next call reloads it."""
    global _RESOLVER, _RESOLVER_LOADED
    _RESOLVER = None
    _RESOLVER_LOADED = False


__all__ = ["LocationResolver", "get_location_resolver", "reset_location_resolver"]