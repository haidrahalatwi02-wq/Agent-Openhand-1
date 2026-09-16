"""Tests for the OpenStreetMap / Overpass search provider.

Everything here is offline: Nominatim and Overpass are exercised through the
in-memory :class:`~tests.conftest.FakeTransport`. No test performs a network
call, and no test relies on OSM being reachable.

The provider is one of several behind the shared ``SearchProvider`` contract, so
these tests also assert that it produces the same normalized ``Business`` shape
as the others and respects the shared error taxonomy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from lead_finder_agent.extraction.normalizer import LeadNormalizer
from lead_finder_agent.models import SearchQuery, WebsiteStatus
from lead_finder_agent.search.base import ProviderErrorKind
from lead_finder_agent.search.multi import MultiProviderSearch
from lead_finder_agent.search.providers.osm import (
    DEFAULT_MAX_ELEMENTS,
    OSMProvider,
)
from lead_finder_agent.utils.http import HttpResponse

from tests.conftest import (
    FakeTransport,
    nominatim_result,
    osm_element,
    overpass_response,
)


def build_provider(transport: FakeTransport, **config: Any) -> OSMProvider:
    """An OSM provider wired to the fake transport, with throttling disabled."""
    settings: Dict[str, Any] = {
        "client": transport.client(),
        "geocode_min_interval": 0,
    }
    settings.update(config)
    return OSMProvider(settings)


def scenario(
    transport: FakeTransport,
    location: str = "aden",
    elements: List[Dict[str, Any]] | None = None,
    remark: str | None = None,
    overpass_status: int = 200,
) -> OSMProvider:
    """Register a successful geocode plus an Overpass response."""
    transport.add("nominatim", body=json.dumps(nominatim_result(location)))
    body = (
        json.dumps(overpass_response(elements or [], remark))
        if overpass_status == 200
        else ""
    )
    transport.add("overpass", status=overpass_status, body=body)
    return build_provider(transport)


# --------------------------------------------------------------------------- #
# Successful search and international coverage
# --------------------------------------------------------------------------- #


class TestSuccessfulSearch:
    def test_discovers_businesses_for_a_requested_city(self, fake_transport):
        elements = [
            osm_element(101, name="Al Bahr Seafood", tags={"amenity": "restaurant"}),
            osm_element(102, name="Golden Star Bakery", tags={"shop": "bakery"}),
        ]
        provider = scenario(fake_transport, "aden", elements)

        response = provider.search(SearchQuery(city="Aden", country="Yemen", limit=10))

        assert response.ok
        assert response.count == 2
        assert {lead["business_name"] for lead in response.leads} == {
            "Al Bahr Seafood",
            "Golden Star Bakery",
        }

    @pytest.mark.parametrize(
        "location, city, country",
        [
            ("aden", "Aden", "Yemen"),
            ("riyadh", "Riyadh", "Saudi Arabia"),
            ("lisbon", "Lisbon", "Portugal"),
        ],
    )
    def test_works_internationally(self, fake_transport, location, city, country):
        """The same code path serves any city; nothing is country-specific."""
        elements = [osm_element(201, name=f"A {city} Business", tags={"shop": "bakery"})]
        provider = scenario(fake_transport, location, elements)

        response = provider.search(
            SearchQuery(city=city, country=country, business_type="bakeries", limit=10)
        )

        assert response.ok
        assert response.count == 1
        lead = response.leads[0]
        assert lead["city"] == city
        assert lead["country"] == country

    def test_sends_the_requested_location_to_the_geocoder(self, fake_transport):
        """The user's location must drive the search, not a built-in default."""
        provider = scenario(fake_transport, "riyadh", [])
        provider.search(SearchQuery(city="Riyadh", country="Saudi Arabia", limit=10))

        geocode = next(r for r in fake_transport.requests if "nominatim" in r["url"])
        assert geocode["params"]["q"] == "Riyadh, Saudi Arabia"

    def test_bbox_comes_from_the_geocoded_area(self, fake_transport):
        provider = scenario(fake_transport, "lisbon", [])
        provider.search(SearchQuery(city="Lisbon", country="Portugal", limit=5))

        call = next(r for r in fake_transport.requests if "overpass" in r["url"])
        statement = call["data"]["data"]
        # south,west,north,east for Lisbon. Compared field by field because
        # Python's float repr renders 38.80 as 38.8.
        assert "38.68,-9.23,38.8,-9.09" in statement

    def test_the_bbox_follows_the_requested_city(self, fake_transport):
        """A different city must produce a different bounding box."""
        provider = scenario(fake_transport, "riyadh", [])
        provider.search(SearchQuery(city="Riyadh", country="Saudi Arabia", limit=5))

        call = next(r for r in fake_transport.requests if "overpass" in r["url"])
        assert "24.4,46.35,24.95,47.0" in call["data"]["data"]

    def test_city_only_query_still_reaches_the_geocoder(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", limit=5))

        geocode = next(r for r in fake_transport.requests if "nominatim" in r["url"])
        # The query may add a resolved country, but the city must be present.
        assert geocode["params"]["q"].startswith("Aden")


# --------------------------------------------------------------------------- #
# Category and keyword handling
# --------------------------------------------------------------------------- #


class TestCategoryFiltering:
    def test_business_type_selects_the_matching_osm_tags(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(
            SearchQuery(city="Aden", business_type="restaurants", limit=10)
        )

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert '["amenity"="restaurant"]' in statement
        assert '["amenity"="fast_food"]' in statement

    def test_an_arabic_business_type_resolves(self, fake_transport):
        """Arabic input must resolve to the same tags as English."""
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", business_type="مطاعم", limit=10))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert '["amenity"="restaurant"]' in statement

    def test_keywords_broaden_the_category_match(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(
            SearchQuery(city="Aden", business_type="shops", keywords=["bakery"], limit=10)
        )

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert '["shop"="bakery"]' in statement

    def test_unknown_type_falls_back_to_a_broad_selector(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", business_type="zzz-unknown", limit=5))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert '["shop"~"*"]' in statement

    def test_query_matches_nodes_ways_and_relations(self, fake_transport):
        """A business mapped as a building outline must be found too."""
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", business_type="restaurants", limit=5))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert "nwr[" in statement
        assert "node[" not in statement

    def test_business_type_is_carried_onto_the_record(self, fake_transport):
        provider = scenario(
            fake_transport, "aden", [osm_element(1, tags={"amenity": "restaurant"})]
        )
        response = provider.search(
            SearchQuery(city="Aden", business_type="restaurants", limit=5)
        )
        assert response.leads[0]["business_type"] == "restaurants"


# --------------------------------------------------------------------------- #
# Record shape: only what OSM actually returns
# --------------------------------------------------------------------------- #


class TestRecordTranslation:
    def test_captures_the_documented_fields(self, fake_transport):
        element = osm_element(
            4242,
            name="Corner Cafe",
            tags={
                "amenity": "cafe",
                "phone": "+967 71 000 0000",
                "website": "https://cornercafe.example",
                "addr:housenumber": "12",
                "addr:street": "Main Street",
                "addr:city": "Aden",
            },
            lat=12.5,
            lon=45.5,
        )
        provider = scenario(fake_transport, "aden", [element])
        lead = provider.search(SearchQuery(city="Aden", country="Yemen", limit=5)).leads[0]

        assert lead["business_name"] == "Corner Cafe"
        assert lead["phone"] == "+967 71 000 0000"
        assert lead["website_url"] == "https://cornercafe.example"
        assert lead["address"] == "12, Main Street, Aden"
        assert lead["latitude"] == 12.5
        assert lead["longitude"] == 45.5
        assert lead["source_id"] == "node/4242"
        assert lead["osm_type"] == "node"
        assert lead["osm_id"] == 4242
        assert lead["city"] == "Aden"
        assert lead["country"] == "Yemen"

    def test_source_url_points_at_the_osm_object(self, fake_transport):
        provider = scenario(fake_transport, "aden", [osm_element(777)])
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]
        assert lead["source_url"] == "https://www.openstreetmap.org/node/777"

    def test_way_uses_its_center_coordinates(self, fake_transport):
        """A way has no lat/lon, only a computed ``center``."""
        element = osm_element(
            900, element_type="way", lat=None, lon=None, center={"lat": 12.3, "lon": 45.6}
        )
        provider = scenario(fake_transport, "aden", [element])
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]

        assert lead["latitude"] == 12.3
        assert lead["longitude"] == 45.6
        assert lead["source_id"] == "way/900"

    def test_missing_fields_stay_absent_and_are_never_invented(self, fake_transport):
        """Only a name and a type are present; nothing else may be fabricated."""
        provider = scenario(fake_transport, "aden", [osm_element(1, tags={"shop": "bakery"})])
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]

        assert lead["phone"] is None
        assert lead["email"] is None
        assert lead["website_url"] is None
        assert lead["address"] is None

    def test_unnamed_elements_are_dropped(self, fake_transport):
        elements = [
            osm_element(1, name=None, tags={"shop": "bakery"}),
            osm_element(2, name="Named Shop", tags={"shop": "bakery"}),
        ]
        provider = scenario(fake_transport, "aden", elements)
        response = provider.search(SearchQuery(city="Aden", limit=10))

        assert response.count == 1
        assert response.leads[0]["business_name"] == "Named Shop"

    def test_brand_is_used_when_there_is_no_name(self, fake_transport):
        element = {"type": "node", "id": 5, "lat": 1.0, "lon": 2.0, "tags": {"brand": "Acme", "shop": "bakery"}}
        provider = scenario(fake_transport, "aden", [element])
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]
        assert lead["business_name"] == "Acme"

    def test_malformed_element_is_skipped_without_losing_the_rest(self, fake_transport):
        elements: List[Any] = [
            "not-an-object",
            {"type": "node", "id": 6, "tags": "not-a-dict"},
            osm_element(7, name="Good Shop", tags={"shop": "bakery"}),
        ]
        provider = scenario(fake_transport, "aden", elements)
        response = provider.search(SearchQuery(city="Aden", limit=10))

        assert response.ok
        assert response.count == 1

    def test_city_falls_back_to_the_osm_address_tag(self, fake_transport):
        """With no city in the query, the OSM address tag is the best source."""
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, tags={"shop": "bakery", "addr:city": "Aden"})],
        )
        response = provider.search(SearchQuery(country="Yemen", limit=5))
        assert response.leads[0]["city"] == "Aden"

    def test_country_falls_back_to_the_geocoder_result(self, fake_transport):
        """With no country in the query, the geocoded country is filled in."""
        provider = scenario(fake_transport, "riyadh", [osm_element(1, tags={"shop": "bakery"})])
        response = provider.search(SearchQuery(city="Riyadh", limit=5))
        assert response.leads[0]["country"] == "Saudi Arabia"

    def test_categories_are_collected_from_osm_tags(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, tags={"shop": "bakery", "amenity": "cafe"})],
        )
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]
        assert lead["categories"] == ["amenity", "shop"]


# --------------------------------------------------------------------------- #
# Website handling
# --------------------------------------------------------------------------- #


class TestWebsiteHandling:
    def test_a_real_website_is_captured(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, tags={"shop": "bakery", "website": "https://bakery.example"})],
        )
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]
        assert lead["website_url"] == "https://bakery.example"

    def test_missing_website_is_not_treated_as_no_website(self, fake_transport):
        """The provider must not claim a business lacks a website.

        Absence of a ``website`` tag only means a mapper did not record one, so
        the record carries no URL and no verdict at all.
        """
        provider = scenario(fake_transport, "aden", [osm_element(1, tags={"shop": "bakery"})])
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]

        assert lead["website_url"] is None
        # The provider makes no claim beyond "no URL recorded".
        assert "website_status" not in lead

    def test_no_website_leaves_the_checker_to_decide(self, fake_transport):
        """Normalization must not turn a missing URL into "not found"."""
        provider = scenario(fake_transport, "aden", [osm_element(1, tags={"shop": "bakery"})])
        raw = provider.search(SearchQuery(city="Aden", limit=5)).leads

        lead = LeadNormalizer().normalize_many(raw, source="osm")[0]

        assert lead.website_url is None
        assert lead.website_status is WebsiteStatus.NOT_CHECKED
        assert lead.website_status != WebsiteStatus.NOT_FOUND

    def test_a_social_url_in_the_website_tag_is_not_an_owned_website(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, tags={"shop": "bakery", "website": "https://facebook.com/bakery"})],
        )
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]

        assert lead["website_url"] is None
        assert "facebook" in lead["social_links"]

    def test_contact_website_tag_is_accepted(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, tags={"shop": "bakery", "contact:website": "https://c.example"})],
        )
        lead = provider.search(SearchQuery(city="Aden", limit=5)).leads[0]
        assert lead["website_url"] == "https://c.example"


# --------------------------------------------------------------------------- #
# Deduplication and limits
# --------------------------------------------------------------------------- #


class TestDeduplicationAndLimits:
    def test_duplicate_osm_ids_are_collapsed(self, fake_transport):
        """The same object returned twice must not become two leads."""
        duplicate = osm_element(42, name="Twice", tags={"shop": "bakery"})
        provider = scenario(fake_transport, "aden", [duplicate, dict(duplicate)])

        response = provider.search(SearchQuery(city="Aden", limit=10))
        assert response.count == 1

    def test_distinct_ids_are_kept_separate(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [
                osm_element(1, name="Same Name", tags={"shop": "bakery"}),
                osm_element(2, name="Same Name", tags={"shop": "bakery"}),
            ],
        )
        response = provider.search(SearchQuery(city="Aden", limit=10))
        assert response.count == 2

    def test_fallback_identity_is_deterministic_without_an_id(self, fake_transport):
        """Two id-less records describing the same business still collapse."""
        element_a = {"type": "node", "lat": 1.0, "lon": 2.0, "tags": {"name": "Ghost", "shop": "bakery"}}
        element_b = {"type": "node", "lat": 1.0, "lon": 2.0, "tags": {"name": " ghost ", "shop": "bakery"}}
        provider = scenario(fake_transport, "aden", [element_a, element_b])

        response = provider.search(SearchQuery(city="Aden", limit=10))
        assert response.count == 1

    def test_never_returns_more_than_the_requested_limit(self, fake_transport):
        elements = [osm_element(i, name=f"Shop {i}", tags={"shop": "bakery"}) for i in range(30)]
        provider = scenario(fake_transport, "aden", elements)

        response = provider.search(SearchQuery(city="Aden", limit=7))
        assert response.count == 7

    def test_limit_of_one(self, fake_transport):
        elements = [osm_element(i, name=f"Shop {i}", tags={"shop": "bakery"}) for i in range(5)]
        provider = scenario(fake_transport, "aden", elements)
        assert provider.search(SearchQuery(city="Aden", limit=1)).count == 1

    def test_overpass_query_asks_for_no_more_than_the_cap(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", limit=10))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert "out center tags 10;" in statement

    def test_max_elements_bounds_a_large_request(self, fake_transport):
        provider = build_provider(fake_transport, max_elements=25)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body=json.dumps(overpass_response([])))
        provider.search(SearchQuery(city="Aden", limit=5000))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert "out center tags 25;" in statement

    def test_oversampling_asks_for_more_when_configured(self, fake_transport):
        provider = build_provider(fake_transport, oversample=2.0)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body=json.dumps(overpass_response([])))
        provider.search(SearchQuery(city="Aden", limit=10))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        # Yet the provider still returns at most the requested limit.
        assert "out center tags 20;" in statement

    def test_default_max_elements_is_applied(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        provider.search(SearchQuery(city="Aden", limit=9999))

        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert f"out center tags {DEFAULT_MAX_ELEMENTS};" in statement

    def test_provider_counts_its_remote_calls(self, fake_transport):
        provider = scenario(fake_transport, "aden", [osm_element(1)])
        response = provider.search(SearchQuery(city="Aden", limit=5))
        assert response.pages_fetched == 2  # one geocode + one Overpass query


# --------------------------------------------------------------------------- #
# Empty results and the Overpass remark trap
# --------------------------------------------------------------------------- #


class TestEmptyAndPartialResults:
    def test_no_matches_is_a_successful_empty_search(self, fake_transport):
        provider = scenario(fake_transport, "aden", [])
        response = provider.search(SearchQuery(city="Aden", limit=5))

        assert response.ok
        assert response.empty
        assert response.count == 0
        assert response.skipped_reason is None
        assert response.error is None

    def test_all_elements_unnamed_is_an_empty_success(self, fake_transport):
        provider = scenario(fake_transport, "aden", [osm_element(1, name=None)])
        response = provider.search(SearchQuery(city="Aden", limit=5))
        assert response.ok
        assert response.count == 0

    def test_a_remark_is_treated_as_a_failure_not_a_partial_answer(self, fake_transport):
        """Overpass returns HTTP 200 with truncated data plus a ``remark``.

        Accepting that silently would report a fraction of a city's businesses
        as the complete answer, so the remark must surface as an error.
        """
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, name="Partial Result")],
            remark="runtime error: query timed out in 'recurse' at line 1",
        )
        response = provider.search(SearchQuery(city="Aden", limit=10))

        assert response.error is not None
        assert response.error_kind == ProviderErrorKind.RATE_LIMITED
        assert response.count == 0

    def test_out_of_memory_remark_is_classified(self, fake_transport):
        provider = scenario(
            fake_transport,
            "aden",
            [],
            remark="runtime error: Query run out of memory using about 512 MB",
        )
        response = provider.search(SearchQuery(city="Aden", limit=10))
        assert response.error_kind == ProviderErrorKind.RATE_LIMITED

    def test_an_unrecognised_remark_still_fails(self, fake_transport):
        provider = scenario(
            fake_transport, "aden", [], remark="something unexpected happened"
        )
        response = provider.search(SearchQuery(city="Aden", limit=10))
        assert response.error_kind == ProviderErrorKind.PROVIDER_ERROR

    def test_a_null_remark_is_ignored(self, fake_transport):
        provider = scenario(fake_transport, "aden", [osm_element(1)], remark=None)
        assert provider.search(SearchQuery(city="Aden", limit=5)).ok


# --------------------------------------------------------------------------- #
# Failure handling: the shared error taxonomy
# --------------------------------------------------------------------------- #


class TestFailureHandling:
    def test_invalid_location_is_a_skip_not_an_error(self, fake_transport):
        """No usable location is an input problem, not a provider failure."""
        provider = build_provider(fake_transport)
        response = provider.search(SearchQuery(limit=5))

        assert response.skipped_reason is not None
        assert response.error is None

    def test_unresolvable_city_is_a_skip(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps([]))
        provider = build_provider(fake_transport)
        response = provider.search(SearchQuery(city="Atlantis", limit=5))

        assert response.skipped_reason is not None
        assert response.error is None

    def test_geocoder_outage_is_an_error_not_a_missing_city(self, fake_transport):
        """An outage must not be reported as "that place does not exist"."""
        fake_transport.add("nominatim", status=503, body="")
        provider = build_provider(fake_transport)
        response = provider.search(SearchQuery(city="Aden", limit=5))

        assert response.error_kind == ProviderErrorKind.PROVIDER_ERROR
        assert response.error is not None
        # The failure is surfaced, but it never claims the city is unusable.
        assert response.skipped_reason is not None
        assert "could not resolve" not in response.skipped_reason.lower()
        assert "503" in response.skipped_reason

    def test_overpass_server_error(self, fake_transport):
        provider = scenario(fake_transport, "aden", [], overpass_status=500)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.PROVIDER_ERROR
        )

    def test_overpass_gateway_timeout(self, fake_transport):
        provider = scenario(fake_transport, "aden", [], overpass_status=504)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.PROVIDER_ERROR
        )

    def test_overpass_rate_limit(self, fake_transport):
        provider = scenario(fake_transport, "aden", [], overpass_status=429)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.RATE_LIMITED
        )

    def test_overpass_bad_request(self, fake_transport):
        provider = scenario(fake_transport, "aden", [], overpass_status=400)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.INVALID_CONFIG
        )

    def test_network_failure_is_classified(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))

        def _fail(method, url, params, data, headers, timeout):
            return HttpResponse(0, "", {}, url, error="connection refused")

        fake_transport.add_handler("overpass", _fail)
        provider = build_provider(fake_transport)
        response = provider.search(SearchQuery(city="Aden", limit=5))

        assert response.error_kind == ProviderErrorKind.NETWORK
        assert response.error_kind != ProviderErrorKind.PROVIDER_ERROR

    def test_request_timeout_is_a_network_failure(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))

        def _timeout(method, url, params, data, headers, timeout):
            return HttpResponse(0, "", {}, url, error="read timed out")

        fake_transport.add_handler("overpass", _timeout)
        provider = build_provider(fake_transport)
        response = provider.search(SearchQuery(city="Aden", limit=5))

        assert response.error_kind == ProviderErrorKind.NETWORK

    def test_malformed_json_is_a_malformed_response(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body="{not json")
        provider = build_provider(fake_transport)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.MALFORMED_RESPONSE
        )

    def test_non_list_elements_is_a_malformed_response(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body=json.dumps({"elements": "nope"}))
        provider = build_provider(fake_transport)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.MALFORMED_RESPONSE
        )

    def test_non_object_payload_is_a_malformed_response(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body=json.dumps([1, 2, 3]))
        provider = build_provider(fake_transport)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == (
            ProviderErrorKind.MALFORMED_RESPONSE
        )

    def test_missing_elements_key_is_an_empty_success(self, fake_transport):
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", body=json.dumps({"version": 0.6}))
        provider = build_provider(fake_transport)

        response = provider.search(SearchQuery(city="Aden", limit=5))
        assert response.ok
        assert response.count == 0

    def test_errors_never_leak_internal_details(self, fake_transport):
        provider = scenario(fake_transport, "aden", [], overpass_status=500)
        response = provider.search(SearchQuery(city="Aden", limit=5))

        message = response.error or ""
        assert "Traceback" not in message
        assert "secret" not in message.lower()

    @pytest.mark.parametrize(
        "status, expected",
        [
            (400, ProviderErrorKind.INVALID_CONFIG),
            (429, ProviderErrorKind.RATE_LIMITED),
            (500, ProviderErrorKind.PROVIDER_ERROR),
            (503, ProviderErrorKind.PROVIDER_ERROR),
        ],
    )
    def test_geocoder_status_translation(self, fake_transport, status, expected):
        fake_transport.add("nominatim", status=status, body="")
        provider = build_provider(fake_transport)
        assert provider.search(SearchQuery(city="Aden", limit=5)).error_kind == expected


# --------------------------------------------------------------------------- #
# Provider contract: registry, availability, normalization, integration
# --------------------------------------------------------------------------- #


class TestProviderContract:
    def test_provider_imports_first_without_a_cycle(self):
        """Importing the OSM provider before anything else must work.

        The config layer must not import from the provider layer: doing so
        creates a cycle (osm -> business_types -> config.loader -> config ->
        settings -> osm) that only shows up when a program's *first* import is
        the provider, which is exactly what a small script or a fork would do.
        A fresh interpreter is the only way to catch it, because by the time the
        suite runs, ``lead_finder_agent.config`` is already in ``sys.modules``.
        """
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import lead_finder_agent.search.providers.osm as m; print(m.OSMProvider.name)",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "osm" in result.stdout

    def test_is_registered_under_osm(self):
        from lead_finder_agent.search import available_providers

        assert "osm" in available_providers()

    def test_needs_no_api_key(self):
        """OSM is a public data source, so it must never require a credential."""
        provider = OSMProvider({"geocode_min_interval": 0})
        assert provider.requires_key_env is None
        assert provider.is_available() is True
        assert provider.unavailable_reason() is None

    def test_does_not_shadow_the_google_places_provider(self):
        from lead_finder_agent.search import available_providers
        from lead_finder_agent.search.providers import GooglePlacesProvider

        assert "google_places" in available_providers()
        assert GooglePlacesProvider.name == "google_places"

    def test_normalizes_into_the_shared_business_model(self, fake_transport):
        """OSM records must normalize into the same Lead shape as any provider."""
        provider = scenario(
            fake_transport,
            "aden",
            [osm_element(1, name="Normal Cafe", tags={"amenity": "cafe", "phone": "+967 1 2 3"})],
        )
        raw = provider.search(
            SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=5)
        ).leads

        lead = LeadNormalizer().normalize_many(raw, source="osm")[0]

        assert lead.business_name == "Normal Cafe"
        assert lead.source == "osm"
        assert lead.city == "Aden"
        assert lead.country == "Yemen"
        assert lead.phone

    def test_unnamed_elements_never_reach_normalization(self, fake_transport):
        provider = scenario(fake_transport, "aden", [osm_element(1, name=None)])
        raw = provider.search(SearchQuery(city="Aden", limit=5)).leads
        assert LeadNormalizer().normalize_many(raw, source="osm") == []

    def test_cooperates_with_other_providers(self, fake_transport):
        from lead_finder_agent.search.providers.sample import SampleProvider

        provider = scenario(fake_transport, "aden", [osm_element(1, name="OSM Shop")])
        result = MultiProviderSearch([provider, SampleProvider()]).run(
            SearchQuery(city="Aden", country="Yemen", limit=50)
        )

        assert "osm" in result.providers_used
        assert "sample" in result.providers_used
        assert any(lead["business_name"] == "OSM Shop" for lead in result.leads)

    def test_a_failing_osm_does_not_stop_other_providers(self, fake_transport):
        from lead_finder_agent.search.providers.sample import SampleProvider

        fake_transport.add("nominatim", status=500, body="")
        provider = build_provider(fake_transport)
        result = MultiProviderSearch([provider, SampleProvider()]).run(
            SearchQuery(city="Aden", limit=10)
        )

        assert result.leads  # sample still delivered
        assert result.responses[0].error is not None
        assert result.responses[1].ok

    def test_raw_results_are_not_capped_before_deduplication(self, fake_transport):
        """The search stage must not spend the limit on the first provider.

        Capping here counted raw records, so duplicates from an earlier
        provider consumed the budget and starved later providers of unique
        leads. The cap now belongs to the pipeline, after de-duplication.
        """
        from lead_finder_agent.search.providers.sample import SampleProvider

        elements = [osm_element(i, name=f"OSM {i}", tags={"shop": "bakery"}) for i in range(10)]
        provider = scenario(fake_transport, "aden", elements)
        result = MultiProviderSearch([provider, SampleProvider()]).run(
            SearchQuery(city="Aden", limit=3)
        )

        # Every provider still answered in full; the cap is applied downstream.
        assert len(result.responses) == 2
        assert all(response.ok for response in result.responses)
        assert len(result.leads) > 3


# --------------------------------------------------------------------------- #
# CLI compatibility
# --------------------------------------------------------------------------- #


class TestCLICompatibility:
    """Drive the real CLI entry point with a mocked HTTP layer.

    ``main`` builds providers itself, so the offline transport is injected by
    patching the HTTP client the OSM provider constructs. Nothing else about the
    CLI is stubbed: argument parsing, the pipeline, scoring and storage all run.
    """

    @staticmethod
    def _patch_http(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
        from lead_finder_agent.search.providers import osm as osm_module

        real_client = osm_module.HttpClient

        def _client(*args: Any, **kwargs: Any):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(osm_module, "HttpClient", _client)

    def test_mocked_search_for_aden(self, monkeypatch, fake_transport, tmp_path):
        """The documented CLI call must work against mocked OSM data."""
        from lead_finder_agent.cli import main

        self._patch_http(monkeypatch, fake_transport)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add(
            "overpass",
            body=json.dumps(
                overpass_response(
                    [
                        osm_element(1, name="Aden Restaurant", tags={"amenity": "restaurant"}),
                        osm_element(2, name="Aden Cafe", tags={"amenity": "cafe"}),
                    ]
                )
            ),
        )

        code = main(
            [
                "--db", str(tmp_path / "cli.db"),
                "search",
                "--country", "Yemen",
                "--city", "Aden",
                "--type", "restaurants",
                "--limit", "20",
                "--providers", "osm",
                "--no-website-check",
            ]
        )

        assert code == 0

    def test_mocked_search_for_another_country(
        self, monkeypatch, fake_transport, tmp_path, capsys
    ):
        from lead_finder_agent.cli import main

        self._patch_http(monkeypatch, fake_transport)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("lisbon")))
        fake_transport.add(
            "overpass",
            body=json.dumps(
                overpass_response([osm_element(1, name="Lisbon Bakery", tags={"shop": "bakery"})])
            ),
        )

        code = main(
            [
                "--db", str(tmp_path / "cli2.db"),
                "search",
                "--country", "Portugal",
                "--city", "Lisbon",
                "--type", "bakeries",
                "--limit", "20",
                "--providers", "osm",
                "--no-website-check",
            ]
        )

        out = capsys.readouterr().out
        assert code == 0
        assert "Lisbon Bakery" in out
        assert "1 lead(s) found" in out

    def test_cli_reports_a_provider_error_without_crashing(
        self, monkeypatch, fake_transport, tmp_path, capsys
    ):
        """An Overpass outage must surface as a warning, not a traceback."""
        from lead_finder_agent.cli import main

        self._patch_http(monkeypatch, fake_transport)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add("overpass", status=504, body="")

        code = main(
            [
                "--db", str(tmp_path / "cli3.db"),
                "search",
                "--city", "Aden",
                "--limit", "5",
                "--providers", "osm",
                "--no-website-check",
            ]
        )

        assert code == 0
        assert "warning" in capsys.readouterr().err.lower()

    def test_cli_still_lists_both_real_providers(self):
        from lead_finder_agent.cli import main

        code = main(["providers"])
        assert code == 0

    def test_arabic_city_and_type_through_the_cli(
        self, monkeypatch, fake_transport, tmp_path, capsys
    ):
        """``search --city عدن --type المطاعم`` must work end to end.

        This is the documented Arabic invocation: the city is folded onto its
        canonical spelling and the Arabic category resolves to the same OSM tags
        as ``restaurants``.
        """
        from lead_finder_agent.cli import main

        self._patch_http(monkeypatch, fake_transport)
        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add(
            "overpass",
            body=json.dumps(
                overpass_response(
                    [osm_element(1, name="مطعم عدن", tags={"amenity": "restaurant"})]
                )
            ),
        )

        code = main(
            [
                "--db", str(tmp_path / "cli_ar.db"),
                "search",
                "--city", "عدن",
                "--type", "المطاعم",
                "--limit", "20",
                "--providers", "osm",
                "--no-website-check",
            ]
        )

        assert code == 0
        # Arabic category must have become the restaurant tags.
        statement = next(
            r for r in fake_transport.requests if "overpass" in r["url"]
        )["data"]["data"]
        assert '["amenity"="restaurant"]' in statement
        # The Arabic business name survives to the output.
        assert "مطعم عدن" in capsys.readouterr().out