"""Offline sample provider.

This provider ships a small set of realistic-looking business records per city.
It exists for three reasons:

* the project is runnable (and testable) with zero network access;
* demos and CI never depend on a third-party API being up;
* it is a working reference implementation for people adding their own
  provider.

Records are fictional. They are **not** real businesses.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from lead_finder_agent.models import ProviderKind, SearchQuery
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.search.business_types import BusinessTypeResolver, categories_match
from lead_finder_agent.search.registry import register_provider
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import normalize_whitespace

log = get_logger("search.providers.sample")


def _record(
    name: str,
    business_type: str,
    city: str,
    country: str,
    *,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    website: Optional[str] = None,
    social: Optional[Dict[str, str]] = None,
    address: Optional[str] = None,
    description: Optional[str] = None,
    rating: Optional[float] = None,
    reviews: Optional[int] = None,
    status: str = "active",
) -> Dict[str, Any]:
    return {
        "source_id": f"sample/{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
        "business_name": name,
        "business_type": business_type,
        "city": city,
        "country": country,
        "phone": phone,
        "email": email,
        "website_url": website,
        "social_links": social or {},
        "address": address,
        "description": description,
        "rating": rating,
        "review_count": reviews,
        "business_status": status,
    }


# Fictional sample data. Keys are lowercase city names.
_SAMPLE_DATA: Dict[str, List[Dict[str, Any]]] = {
    "aden": [
        _record(
            "Al Bahr Seafood Restaurant",
            "restaurant",
            "Aden",
            "Yemen",
            phone="+967 71 234 5678",
            social={"facebook": "https://facebook.com/albahrseafood"},
            address="Corniche Road, Aden",
            description="Family seafood restaurant on the Aden corniche.",
            rating=4.4,
            reviews=120,
        ),
        _record(
            "Aden Fashion House",
            "clothing",
            "Aden",
            "Yemen",
            phone="+967 73 555 1200",
            address="Main Street, Crater, Aden",
            description="Clothing and accessories for men and women.",
            rating=4.1,
            reviews=34,
        ),
        _record(
            "Modern Electronics Aden",
            "electronics",
            "Aden",
            "Yemen",
            phone="+967 71 900 4433",
            email="info@modernelectronics-aden.example",
            website="https://modernelectronics-aden.example",
            address="Al Mualla, Aden",
            description="Consumer electronics and home appliances retailer.",
            rating=4.6,
            reviews=210,
        ),
        _record(
            "Golden Star Bakery",
            "bakery",
            "Aden",
            "Yemen",
            phone="+967 72 333 8899",
            social={"instagram": "https://instagram.com/goldenstarbakery"},
            address="Sheikh Othman, Aden",
            description="Neighbourhood bakery and pastry shop.",
            rating=4.7,
            reviews=58,
        ),
        _record(
            "Aden Auto Care",
            "car_repair",
            "Aden",
            "Yemen",
            phone="+967 71 111 2020",
            address="Industrial Area, Aden",
            description="Car repair and maintenance workshop.",
            rating=4.0,
            reviews=22,
        ),
        _record(
            "Fahd Mini Market",
            "grocery",
            "Aden",
            "Yemen",
            phone="+967 73 777 4545",
            address="Khor Maksar, Aden",
            description="Neighbourhood grocery and convenience store.",
            reviews=5,
        ),
        _record(
            "Al Noor Furniture",
            "furniture",
            "Aden",
            "Yemen",
            phone="+967 71 654 3210",
            address="Al Mansoura, Aden",
            description="Home and office furniture showroom.",
            rating=3.9,
            reviews=41,
        ),
        _record(
            "Siraji Beauty Salon",
            "beauty",
            "Aden",
            "Yemen",
            phone="+967 73 210 9090",
            social={"instagram": "https://instagram.com/sirajibeauty"},
            address="Crater, Aden",
            description="Ladies beauty salon and spa.",
            rating=4.8,
            reviews=96,
        ),
    ],
    "sanaa": [
        _record(
            "Sanaa Textile Market",
            "clothing",
            "Sanaa",
            "Yemen",
            phone="+967 71 444 7788",
            address="Bab Al Yemen, Sanaa",
            description="Textiles, fabrics and ready-made clothing.",
            rating=4.2,
            reviews=76,
        ),
        _record(
            "Bab Al Yemen Restaurant",
            "restaurant",
            "Sanaa",
            "Yemen",
            phone="+967 73 123 4567",
            social={"facebook": "https://facebook.com/babalyemen"},
            address="Old City, Sanaa",
            description="Traditional Yemeni cuisine in the old city.",
            rating=4.5,
            reviews=302,
        ),
        _record(
            "Sanaa Computer Center",
            "electronics",
            "Sanaa",
            "Yemen",
            phone="+967 71 888 1122",
            email="sales@sanaacomputer.example",
            website="https://sanaacomputer.example",
            address="Hadda Street, Sanaa",
            description="Computers, accessories and IT services.",
            rating=4.3,
            reviews=88,
        ),
        _record(
            "Al Saeed Pharmacy",
            "pharmacy",
            "Sanaa",
            "Yemen",
            phone="+967 71 222 3344",
            address="Al Tahreer Square, Sanaa",
            description="Community pharmacy with delivery.",
            rating=4.4,
            reviews=63,
        ),
        _record(
            "Yemen Gold Jewelry",
            "jewelry",
            "Sanaa",
            "Yemen",
            phone="+967 73 606 5050",
            social={"facebook": "https://facebook.com/yemengold"},
            address="Al Zubairi Street, Sanaa",
            description="Gold and silver jewelry workshop and showroom.",
            rating=4.6,
            reviews=145,
        ),
    ],
    "taiz": [
        _record(
            "Taiz Electronics Souq",
            "electronics",
            "Taiz",
            "Yemen",
            phone="+967 71 303 9090",
            address="Souq Street, Taiz",
            description="Mobile phones and electronics accessories.",
            rating=4.1,
            reviews=47,
        ),
        _record(
            "Taiz Modern Restaurant",
            "restaurant",
            "Taiz",
            "Yemen",
            phone="+967 73 456 1212",
            address="Al Mudhaffar, Taiz",
            description="Grills and traditional dishes.",
            reviews=9,
        ),
        _record(
            "Al Aqeeq Furniture Taiz",
            "furniture",
            "Taiz",
            "Yemen",
            phone="+967 71 777 6060",
            address="Jamal Street, Taiz",
            description="Handmade wooden furniture.",
            rating=4.5,
            reviews=51,
        ),
    ],
    "hodeidah": [
        _record(
            "Hodeidah Car Workshop",
            "car_repair",
            "Hodeidah",
            "Yemen",
            phone="+967 71 505 7070",
            address="Industrial Zone, Hodeidah",
            description="Auto repair, bodywork and painting.",
            rating=4.0,
            reviews=18,
        ),
        _record(
            "Red Sea Fish Market Shop",
            "grocery",
            "Hodeidah",
            "Yemen",
            phone="+967 73 808 3030",
            address="Port Road, Hodeidah",
            description="Fresh fish and seafood retailer.",
            reviews=6,
        ),
        _record(
            "Hodeidah Fashion",
            "clothing",
            "Hodeidah",
            "Yemen",
            phone="+967 71 232 4343",
            social={"facebook": "https://facebook.com/hodeidahfashion"},
            address="Market Street, Hodeidah",
            description="Modern clothing store.",
            rating=4.2,
            reviews=29,
        ),
    ],
}


@register_provider
class SampleProvider(BaseSearchProvider):
    """Returns fictional offline sample data for known cities."""

    name = "sample"
    kind = ProviderKind.SAMPLE
    description = "Offline fictional sample data - always available, no network"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.data: Dict[str, List[Dict[str, Any]]] = self.config.get("data") or _SAMPLE_DATA
        # Shared with the OSM provider so free text (including Arabic) resolves
        # to the same category, and the offline demo behaves like the real one.
        self.resolver = self.config.get("resolver") or BusinessTypeResolver.load(
            self.config.get("business_types_path")
        )

    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        city = (query.city or self.config.get("default_city") or "Aden").strip().lower()
        records: Iterable[Dict[str, Any]] = self.data.get(city, [])

        if city not in self.data:
            log.info("No sample data for city %r", query.city)
            records = []

        matched = [dict(record) for record in records if self._matches(record, query)]
        return matched[: query.limit]

    def _matches(self, record: Dict[str, Any], query: SearchQuery) -> bool:
        if not self._matches_country(record, query):
            return False

        needle = " ".join(
            filter(None, [query.business_type or ""] + list(query.keywords or []))
        ).strip().lower()
        if not needle:
            return True

        # A resolvable category is the strongest signal: "محلات ملابس" and
        # "clothing shops" both map to the `clothing` category.
        category = self.resolver.category_for(needle)
        record_type = str(record.get("business_type") or "").lower()
        if category and categories_match(category, record_type):
            return True

        haystack = " ".join(
            str(record.get(field) or "")
            for field in ("business_name", "business_type", "description", "categories")
        ).lower()

        # Every token must appear somewhere; a trailing "s" is ignored so
        # "restaurants" still matches a record typed as "restaurant".
        return all(self._token_in(token, haystack) for token in needle.split())

    @staticmethod
    def _matches_country(record: Dict[str, Any], query: SearchQuery) -> bool:
        """Filter by country when the caller supplied one.

        The sample records are grouped by city and every city here sits in one
        country, so an explicit ``--country`` that disagrees with the record must
        exclude it rather than silently returning it.
        """
        requested = normalize_whitespace(query.country)
        if not requested:
            return True
        actual = normalize_whitespace(record.get("country"))
        if not actual:
            return True
        return actual.lower() == requested.lower()

    @staticmethod
    def _token_in(token: str, haystack: str) -> bool:
        if token in haystack:
            return True
        if len(token) > 3 and token.endswith("s") and token[:-1] in haystack:
            return True
        return False


__all__ = ["SampleProvider"]
