"""OpenStreetMap provider (free, no API key required).

Flow:
1. Geocode the requested city with Nominatim to get a bounding box.
2. Query the Overpass API for matching shops/amenities inside that box.
3. Emit one raw record per element, including its public tags.

Both services are free and open, but they are shared community resources:
requests are kept small, a descriptive ``User-Agent`` is sent, and repeated
geocoding is throttled. No credential is involved, which is why this provider
declares no ``requires_key_env``.

Honest data only
----------------
OpenStreetMap is a volunteer-mapped geographic database, not a business
registry. Only tags that are actually present become fields; anything missing
is left absent rather than guessed. In particular **a missing ``website`` tag
proves nothing**: most mappers simply do not record one. The provider therefore
never asserts that a business has no website -- it returns no website URL and
leaves the verdict to the Website Checker, which keeps ``website_not_found``
and ``website_unknown`` distinct.

Overpass caveats
----------------
Overpass reports some failures (query timeout, out of memory, server too busy)
as a **HTTP 200 response with a top-level ``remark``**, possibly with truncated
data. Checking only the status code would silently accept a partial answer, so
an ``remark`` is treated as a failure and classified through the provider error
taxonomy. Overpass has no pagination token: a bounding-box query is a single
logical request and the result set is capped client-side.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from lead_finder_agent.models import ProviderKind, SearchQuery
from lead_finder_agent.search.base import (
    BaseSearchProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderSkip,
)
from lead_finder_agent.search.business_types import BusinessTypeResolver
from lead_finder_agent.search.registry import register_provider
from lead_finder_agent.utils.http import HttpClient
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.providers.osm")

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org"

#: Server-side query budget, in seconds, declared inside the Overpass query.
DEFAULT_OVERPASS_TIMEOUT = 25

#: Upper bound on elements requested from Overpass, so one huge city cannot
#: pull thousands of rows through a shared community endpoint.
DEFAULT_MAX_ELEMENTS = 200

#: Named elements are dropped (an unnamed OSM object is not a usable lead), so a
#: query can legitimately return fewer rows than requested. Raising this factor
#: fetches extra rows to compensate. It defaults to 1 because Overpass is a
#: donation-funded service and over-fetching costs it real work.
DEFAULT_OVERSAMPLE = 1.0

#: Nominatim asks for at most one request per second from a single client.
DEFAULT_GEOCODE_INTERVAL = 1.1

#: Statuses that will never succeed as written (bad query, blocked client).
_AUTH_STATUSES = (400, 401, 403)
_RATE_LIMIT_STATUS = 429

#: Words in an Overpass ``remark`` that mean "retry with a smaller query".
_TRANSIENT_REMARK_HINTS = (
    "timed out",
    "timeout",
    "too busy",
    "out of memory",
    "memory",
    "quota",
    "slot",
)

#: Geocode cache and throttle are module level on purpose: a single process
#: querying several cities should not re-ask Nominatim for the same place, and
#: the throttle is a politeness obligation toward a free service.
_GEOCODE_CACHE: Dict[str, Any] = {}
_LAST_GEOCODE_AT = 0.0


def reset_geocode_cache() -> None:
    """Clear the Nominatim cache and throttle (used by tests)."""
    global _LAST_GEOCODE_AT
    _GEOCODE_CACHE.clear()
    _LAST_GEOCODE_AT = 0.0


@register_provider
class OSMProvider(BaseSearchProvider):
    """Discovers businesses from OpenStreetMap data."""

    name = "osm"
    kind = ProviderKind.API
    description = "OpenStreetMap / Overpass - free, no key required"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = self.config
        self.overpass_url = cfg.get("overpass_url") or OVERPASS_ENDPOINT
        self.nominatim_url = cfg.get("nominatim_url") or NOMINATIM_ENDPOINT
        self.client: HttpClient = cfg.get("client") or HttpClient(
            user_agent=cfg.get("user_agent") or "LeadFinderAgent/0.1",
            timeout=float(cfg.get("http_timeout") or 25.0),
        )
        self.resolver = cfg.get("resolver") or BusinessTypeResolver.load(
            cfg.get("business_types_path")
        )
        self.default_country = cfg.get("default_country")
        self.default_city = cfg.get("default_city")
        self.overpass_timeout = int(cfg.get("overpass_timeout") or DEFAULT_OVERPASS_TIMEOUT)
        self.max_elements = max(1, int(cfg.get("max_elements") or DEFAULT_MAX_ELEMENTS))
        self.oversample = max(1.0, float(cfg.get("oversample") or DEFAULT_OVERSAMPLE))
        self.geocode_min_interval = float(
            cfg.get("geocode_min_interval", DEFAULT_GEOCODE_INTERVAL)
        )

    # -- public API --------------------------------------------------------

    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """Return raw business records for ``query``."""
        city = query.city or self.default_city
        country = query.country or self.default_country
        if not city and not country:
            raise ProviderSkip("OSM provider needs at least a city or a country")

        # An unknown place is an input problem; an unreachable geocoder is not.
        area = self._geocode(city, country)
        if area is None:
            raise ProviderSkip(f"Could not resolve location for city={city!r} country={country!r}")

        wanted = max(1, int(query.limit))
        tags = self.resolver.resolve(query.business_type, query.keywords)
        fetch_cap = self._fetch_cap(wanted)
        statement = self._build_overpass_query(tags, area, fetch_cap, self.overpass_timeout)

        response = self.client.post(self.overpass_url, data={"data": statement})
        self._note_page()
        self._raise_for_status(response, "Overpass")

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any parse failure is unusable
            raise ProviderError(
                f"Overpass returned invalid JSON: {type(exc).__name__}",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError(
                "Overpass returned an unexpected payload type",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            )

        self._raise_for_remark(payload.get("remark"), len(payload.get("elements") or []))

        elements = payload.get("elements")
        if elements is None:
            elements = []
        if not isinstance(elements, list):
            raise ProviderError(
                "'elements' was not a list; the Overpass response was not understood",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            )

        collected: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for element in elements:
            if not isinstance(element, dict):
                # One malformed entry must not discard the rest of the response.
                log.debug("Skipping non-object OSM element: %s", type(element).__name__)
                continue
            record = self._to_raw(element, query, area)
            if record is None:
                continue
            # OSM object ids are stable and permanent, so they are the primary
            # identity. A record without one falls back to a deterministic key
            # built from public fields, mirroring the other providers.
            dedupe_key = record.get("source_id") or self._fallback_key(record)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            collected.append(record)

        # Never hand back more than was asked for, whatever Overpass returned.
        return collected[:wanted]

    def _fetch_cap(self, wanted: int) -> int:
        """How many elements to ask Overpass for.

        Slightly more than ``wanted`` when oversampling is configured, because
        unnamed and duplicate elements are dropped after the response arrives.
        Always bounded by ``max_elements``.
        """
        return max(1, min(self.max_elements, int(wanted * self.oversample)))

    # -- failure classification --------------------------------------------

    def _raise_for_status(self, response: Any, service: str) -> None:
        """Translate a transport or HTTP failure into the error taxonomy.

        ``service`` names the upstream (``Overpass``/``Nominatim``) so the
        message tells the operator which of the two calls actually failed.
        """
        if response.error:
            raise ProviderError(
                f"{service} request failed: {response.error}",
                kind=ProviderErrorKind.NETWORK,
            )

        status = response.status_code
        if status == 200:
            return
        if status in _AUTH_STATUSES:
            raise ProviderError(
                f"{service} rejected the request with HTTP {status}; "
                "the query may be malformed or this client blocked",
                kind=ProviderErrorKind.INVALID_CONFIG,
            )
        if status == _RATE_LIMIT_STATUS:
            raise ProviderError(
                f"{service} rate limited the request (HTTP 429); retry later",
                kind=ProviderErrorKind.RATE_LIMITED,
            )
        if 500 <= status < 600:
            raise ProviderError(
                f"{service} returned HTTP {status}; the service is unavailable",
                kind=ProviderErrorKind.PROVIDER_ERROR,
                retryable=True,
            )
        raise ProviderError(
            f"{service} returned HTTP {status}",
            kind=ProviderErrorKind.PROVIDER_ERROR,
        )

    def _raise_for_remark(self, remark: Any, element_count: int) -> None:
        """Treat an Overpass ``remark`` as a failed query.

        Overpass answers HTTP 200 even when it gave up partway through, putting
        the explanation in ``remark``. Accepting that as a complete answer is
        how a caller ends up quietly reporting a fraction of the businesses in
        a city, so the remark is raised instead.
        """
        if not remark:
            return

        text = str(remark)
        lowered = text.lower()
        kind = (
            ProviderErrorKind.RATE_LIMITED
            if any(hint in lowered for hint in _TRANSIENT_REMARK_HINTS)
            else ProviderErrorKind.PROVIDER_ERROR
        )
        raise ProviderError(
            f"Overpass reported an incomplete result after {element_count} element(s): {text}",
            kind=kind,
        )

    # -- geocoding ---------------------------------------------------------

    def _geocode(self, city: Optional[str], country: Optional[str]) -> Optional[Dict[str, Any]]:
        """Resolve a city to a bounding box, with an in-process cache.

        Returns ``None`` when the location simply does not exist. A geocoder
        that fails for any other reason raises, so an outage is not misreported
        to the user as "this city was not found".
        """
        global _LAST_GEOCODE_AT
        key = f"{self.nominatim_url}|{city}|{country}".lower()
        if key in _GEOCODE_CACHE:
            return _GEOCODE_CACHE[key]

        query = ", ".join(part for part in (city, country) if part)
        if self.geocode_min_interval > 0:
            elapsed = time.monotonic() - _LAST_GEOCODE_AT
            if elapsed < self.geocode_min_interval:
                time.sleep(self.geocode_min_interval - elapsed)

        response = self.client.get(
            f"{self.nominatim_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"Accept": "application/json"},
        )
        _LAST_GEOCODE_AT = time.monotonic()
        self._note_page()
        self._raise_for_status(response, "Nominatim")

        try:
            results = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"Nominatim returned invalid JSON: {type(exc).__name__}",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            ) from exc

        if not isinstance(results, list):
            raise ProviderError(
                "Nominatim returned an unexpected payload type",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            )
        if not results:
            return None

        first = results[0]
        if not isinstance(first, dict):
            return None
        box = first.get("boundingbox")
        if not box or len(box) != 4:
            return None
        try:
            south, north, west, east = (float(value) for value in box)
        except (TypeError, ValueError):
            return None

        area = {
            "south": south,
            "north": north,
            "west": west,
            "east": east,
            "display_name": first.get("display_name"),
            "lat": first.get("lat"),
            "lon": first.get("lon"),
        }
        _GEOCODE_CACHE[key] = area
        return area

    # -- query building ----------------------------------------------------

    @staticmethod
    def _build_overpass_query(
        tags: List[str], area: Dict[str, Any], limit: int, timeout: int = DEFAULT_OVERPASS_TIMEOUT
    ) -> str:
        """Build an Overpass QL statement for ``tags`` inside ``area``.

        ``nwr`` matches nodes, ways and relations in one clause, so a business
        mapped as a building outline or a multipolygon is found too, not only
        the ones mapped as a single point.
        """
        bbox = f"{area['south']},{area['west']},{area['north']},{area['east']}"
        clauses = []
        for tag in tags:
            if "=" not in tag:
                continue
            key, _, value = tag.partition("=")
            key = key.strip()
            value = value.strip()
            selector = f'["{key}"~"{value}"]' if value in ("*", "") else f'["{key}"="{value}"]'
            clauses.append(f"  nwr{selector}({bbox});")
        if not clauses:
            return f"[out:json][timeout:{timeout}];out;"
        body = "\n".join(clauses)
        # Cap the output so a large city does not return thousands of rows.
        return (
            f"[out:json][timeout:{timeout}];\n"
            "(\n"
            f"{body}\n"
            ");\n"
            f"out center tags {max(1, limit)};"
        )

    # -- translation -------------------------------------------------------

    def _to_raw(
        self, element: Dict[str, Any], query: SearchQuery, area: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Translate one OSM element into a raw record.

        Every value comes from the element itself or from the resolved search
        area. Nothing is inferred: an absent tag stays absent.
        """
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            return None
        name = self._first_of(tags, "name", "name:en", "name:ar", "brand", "operator")
        if not name:
            return None  # an unnamed object is not an actionable lead
        name = str(name).strip()
        if not name:
            return None

        lat = element.get("lat")
        lon = element.get("lon")
        if lat is None or lon is None:
            center = element.get("center") or {}
            if isinstance(center, dict):
                lat = center.get("lat")
                lon = center.get("lon")

        website = self._first_website(tags)
        social = self._social_links(tags)
        address = self._address(tags)
        phone = self._first_of(
            tags, "phone", "contact:phone", "contact:mobile", "mobile"
        )
        email = self._first_of(tags, "email", "contact:email")
        description = self._first_of(tags, "description", "description:en", "description:ar")
        categories = sorted(
            key
            for key in tags
            if key in {"shop", "amenity", "office", "tourism", "leisure", "craft", "healthcare"}
        )

        element_type = element.get("type", "node")
        element_id = element.get("id")

        return {
            "source_id": f"{element_type}/{element_id}",
            "business_name": name,
            "business_type": query.business_type
            or self._category_value(tags)
            or (categories[0] if categories else None),
            # The caller's resolved location is authoritative; the OSM address
            # tags are only a fallback, because ``addr:city`` is sometimes a
            # district rather than the city that was searched for.
            "city": query.city or self._city_from_area(area) or self._first_of(tags, "addr:city"),
            "country": query.country
            or self._first_of(tags, "addr:country")
            or self._country_from_area(area),
            "address": address,
            "phone": phone,
            "email": email,
            # Deliberately None when OSM has no website tag: absence of a tag is
            # not evidence that the business has no website.
            "website_url": website,
            "social_links": social,
            "description": description,
            "latitude": lat,
            "longitude": lon,
            "categories": categories,
            "source_url": f"https://www.openstreetmap.org/{element_type}/{element_id}",
            "operator": tags.get("operator"),
            "brand": tags.get("brand"),
            "osm_type": element_type,
            "osm_id": element_id,
        }

    @staticmethod
    def _fallback_key(record: Dict[str, Any]) -> str:
        """Deterministic identity for a record OSM gave no object id."""
        parts = [
            str(record.get("business_name") or ""),
            str(record.get("address") or ""),
            str(record.get("city") or ""),
            str(record.get("country") or ""),
        ]
        normalized = "|".join(" ".join(part.lower().split()) for part in parts)
        return f"fallback:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _category_value(tags: Dict[str, Any]) -> Optional[str]:
        for key in ("shop", "amenity", "office", "tourism", "leisure", "craft", "healthcare"):
            if tags.get(key):
                return str(tags[key])
        return None

    @staticmethod
    def _first_of(tags: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            value = tags.get(key)
            if value:
                return str(value)
        return None

    def _first_website(self, tags: Dict[str, Any]) -> Optional[str]:
        website = self._first_of(tags, "website", "contact:website", "url", "website:official")
        if not website:
            return None
        # A social URL in the website tag is not an owned website.
        if any(host in website.lower() for host in _SOCIAL_HOSTS):
            return None
        return website

    def _social_links(self, tags: Dict[str, Any]) -> Dict[str, str]:
        social = {}
        for key, label in (
            ("contact:facebook", "facebook"),
            ("facebook", "facebook"),
            ("contact:instagram", "instagram"),
            ("instagram", "instagram"),
            ("contact:twitter", "twitter"),
            ("contact:whatsapp", "whatsapp"),
        ):
            if tags.get(key):
                social[label] = str(tags[key])
        website = self._first_of(tags, "website", "contact:website")
        if website:
            lowered = website.lower()
            for host, label in _SOCIAL_HOST_LABELS:
                if host in lowered:
                    social.setdefault(label, website)
        return social

    @staticmethod
    def _address(tags: Dict[str, Any]) -> Optional[str]:
        parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:district"),
            tags.get("addr:city"),
        ]
        joined = ", ".join(str(p).strip() for p in parts if p)
        return joined or None

    @staticmethod
    def _city_from_area(area: Dict[str, Any]) -> Optional[str]:
        display = area.get("display_name") or ""
        return display.split(",")[0].strip() if display else None

    @staticmethod
    def _country_from_area(area: Dict[str, Any]) -> Optional[str]:
        """Last component of a Nominatim display name, e.g. ``Aden, Yemen``."""
        display = area.get("display_name") or ""
        parts = [part.strip() for part in display.split(",") if part.strip()]
        return parts[-1] if len(parts) > 1 else None


#: Hosts that represent a social presence, never an owned website.
_SOCIAL_HOSTS = ("facebook.", "instagram.", "twitter.", "x.com", "tiktok.", "linkedin.")

_SOCIAL_HOST_LABELS = (
    ("facebook.", "facebook"),
    ("instagram.", "instagram"),
    ("twitter.", "twitter"),
    ("x.com", "twitter"),
    ("tiktok.", "tiktok"),
)


__all__ = [
    "OSMProvider",
    "OVERPASS_ENDPOINT",
    "NOMINATIM_ENDPOINT",
    "DEFAULT_MAX_ELEMENTS",
    "DEFAULT_OVERPASS_TIMEOUT",
    "reset_geocode_cache",
]
