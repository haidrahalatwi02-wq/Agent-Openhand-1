"""OpenStreetMap provider (free, no API key required).

Flow:
1. Geocode the requested city with Nominatim to get a bounding box.
2. Query the Overpass API for matching shops/amenities inside that box.
3. Emit one raw record per element, including its public tags.

Both services are free and open, but they are shared community resources:
requests are kept small and a descriptive ``User-Agent`` is sent (see
``LEAD_FINDER_USER_AGENT``). Only one request per stage is made per query.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from lead_finder_agent.models import ProviderKind, SearchQuery
from lead_finder_agent.search.base import BaseSearchProvider, ProviderSkip
from lead_finder_agent.search.business_types import BusinessTypeResolver
from lead_finder_agent.search.registry import register_provider
from lead_finder_agent.utils.http import HttpClient
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.providers.osm")

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org"

# Throttle repeated geocoding calls within a process (Nominatim asks for <=1/s).
_GEOCODE_CACHE: Dict[str, Any] = {}
_LAST_GEOCODE_AT = 0.0
_MIN_GEOCODE_INTERVAL = 1.1


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

    # -- public API --------------------------------------------------------

    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        city = query.city or self.default_city
        country = query.country or self.default_country
        if not city and not country:
            raise ProviderSkip("OSM provider needs at least a city or a country")

        area = self._geocode(city, country)
        if area is None:
            raise ProviderSkip(f"Could not resolve location for city={city!r} country={country!r}")

        tags = self.resolver.resolve(query.business_type, query.keywords)
        statement = self._build_overpass_query(tags, area, query.limit)

        response = self.client.post(self.overpass_url, data={"data": statement})
        if response.error:
            raise ProviderSkip(f"Overpass request failed: {response.error}")
        if response.status_code != 200:
            raise ProviderSkip(f"Overpass returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderSkip(f"Overpass returned invalid JSON: {exc}") from exc

        elements = (payload or {}).get("elements") or []
        leads = [self._to_raw(element, query, area) for element in elements]
        return [lead for lead in leads if lead is not None]

    # -- geocoding ---------------------------------------------------------

    def _geocode(self, city: Optional[str], country: Optional[str]) -> Optional[Dict[str, Any]]:
        """Resolve a city to a bounding box, with an in-process cache."""
        global _LAST_GEOCODE_AT
        key = f"{city}|{country}".lower()
        if key in _GEOCODE_CACHE:
            return _GEOCODE_CACHE[key]

        query = ", ".join(part for part in (city, country) if part)
        elapsed = time.monotonic() - _LAST_GEOCODE_AT
        if elapsed < _MIN_GEOCODE_INTERVAL:
            time.sleep(_MIN_GEOCODE_INTERVAL - elapsed)

        response = self.client.get(
            f"{self.nominatim_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"Accept": "application/json"},
        )
        _LAST_GEOCODE_AT = time.monotonic()

        if response.error or response.status_code != 200:
            log.warning("Geocoding failed for %r: %s", query, response.error or response.status_code)
            return None
        try:
            results = response.json()
        except Exception:  # noqa: BLE001
            return None
        if not results:
            return None

        first = results[0]
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
    def _build_overpass_query(tags: List[str], area: Dict[str, Any], limit: int) -> str:
        bbox = f"{area['south']},{area['west']},{area['north']},{area['east']}"
        clauses = []
        for tag in tags:
            if "=" not in tag:
                continue
            key, _, value = tag.partition("=")
            key = key.strip()
            value = value.strip()
            selector = f'["{key}"~"{value}"]' if value in ("*", "") else f'["{key}"="{value}"]'
            clauses.append(f"  node{selector}({bbox});")
            clauses.append(f"  way{selector}({bbox});")
        if not clauses:
            return "[out:json][timeout:25];out;"
        body = "\n".join(clauses)
        # Cap the output so a large city does not return thousands of rows.
        return (
            "[out:json][timeout:25];\n"
            "(\n"
            f"{body}\n"
            ");\n"
            f"out center tags {max(1, limit)};"
        )

    # -- translation -------------------------------------------------------

    def _to_raw(
        self, element: Dict[str, Any], query: SearchQuery, area: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        tags = element.get("tags") or {}
        name = (tags.get("name") or tags.get("name:en") or tags.get("name:ar") or "").strip()
        if not name:
            return None  # an unnamed node is not an actionable lead

        lat = element.get("lat")
        lon = element.get("lon")
        if lat is None or lon is None:
            center = element.get("center") or {}
            lat = center.get("lat")
            lon = center.get("lon")

        website = self._first_website(tags)
        social = self._social_links(tags)
        address = self._address(tags)
        phone = self._first_of(tags, "phone", "contact:phone", "contact:mobile", "mobile")
        email = self._first_of(tags, "email", "contact:email")
        description = self._first_of(tags, "description", "description:en", "description:ar")
        categories = sorted(k for k in tags if k in {"shop", "amenity", "office", "tourism", "leisure", "craft", "healthcare"})

        return {
            "source_id": f"{element.get('type', 'node')}/{element.get('id')}",
            "business_name": name,
            "business_type": query.business_type
            or self._category_value(tags)
            or (categories[0] if categories else None),
            "city": query.city or self._city_from_area(area),
            "country": query.country,
            "address": address,
            "phone": phone,
            "email": email,
            "website_url": website,
            "social_links": social,
            "description": description,
            "latitude": lat,
            "longitude": lon,
            "categories": categories,
            "source_url": f"https://www.openstreetmap.org/{element.get('type', 'node')}/{element.get('id')}",
            "operator": tags.get("operator"),
            "brand": tags.get("brand"),
        }

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
        website = self._first_of(
            tags, "website", "contact:website", "url", "website:official"
        )
        if not website:
            return None
        # A social URL in the website tag is not an owned website.
        lowered = website.lower()
        if any(host in lowered for host in ("facebook.", "instagram.", "twitter.", "x.com", "tiktok.")):
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
            for host, label in (
                ("facebook.", "facebook"),
                ("instagram.", "instagram"),
                ("twitter.", "twitter"),
                ("x.com", "twitter"),
                ("tiktok.", "tiktok"),
            ):
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


__all__ = ["OSMProvider", "OVERPASS_ENDPOINT", "NOMINATIM_ENDPOINT"]
