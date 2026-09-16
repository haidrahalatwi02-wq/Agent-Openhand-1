"""Tests for the Google Places (New) provider.

Every network call is faked. No test in this module touches the internet, and
no real credential is ever used: the fixtures set a clearly-fake key so the
"unavailable" path can be tested separately.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from lead_finder_agent.models import SearchQuery
from lead_finder_agent.search.base import ProviderErrorKind
from lead_finder_agent.search.providers.google_places import (
    DEFAULT_FIELD_MASK,
    MAX_PAGE_SIZE,
    GooglePlacesProvider,
)
from lead_finder_agent.utils.http import HttpClient, HttpResponse
from tests.conftest import google_page, google_place


class RecordingTransport:
    """Returns queued pages and records exactly what was sent.

    Each response is a ``(status, body)`` pair, or a callable taking the decoded
    request body so a test can vary the answer by ``pageToken``.
    """

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def __call__(self, method, url, params, data, headers, timeout) -> HttpResponse:
        body: Dict[str, Any] = {}
        if isinstance(data, str) and data:
            try:
                body = json.loads(data)
            except ValueError:
                body = {"_raw": data}
        elif isinstance(data, dict):
            body = dict(data)

        self.requests.append({"method": method, "url": url, "body": body, "headers": headers})

        if not self._responses:
            return HttpResponse(200, json.dumps({"places": []}), {}, url)

        response = self._responses.pop(0)
        if callable(response):
            return response(body, headers, url)
        status, payload = response
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return HttpResponse(status, text, {"content-type": "application/json"}, url)

    def client(self) -> HttpClient:
        return HttpClient(transport=self)


def make_provider(transport: RecordingTransport, **config: Any) -> GooglePlacesProvider:
    """Build a provider wired to a recording transport with a fake key."""
    config.setdefault("api_key", "fake-key")
    config["client"] = transport.client()
    return GooglePlacesProvider(config)


# --------------------------------------------------------------------------- #
# Successful search
# --------------------------------------------------------------------------- #


class TestSuccessfulSearch:
    def test_returns_normalizable_records(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place("a", "Alpha Cafe")]))])
        response = make_provider(transport).search(
            SearchQuery(country="Yemen", city="Aden", business_type="restaurants", limit=20)
        )

        assert response.ok
        assert response.count == 1
        record = response.leads[0]
        assert record["business_name"] == "Alpha Cafe"
        assert record["source_id"] == "a"
        assert record["city"] == "Aden"
        assert record["country"] == "Yemen"
        assert record["business_type"] == "restaurant"
        assert record["latitude"] == 12.7855
        assert record["source_url"].startswith("https://maps.google.com/")

    def test_builds_query_from_caller_input(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place()]))])
        make_provider(transport).search(
            SearchQuery(country="Portugal", city="Lisbon", business_type="bakeries", limit=5)
        )
        assert transport.requests[0]["body"]["textQuery"] == "bakeries in Lisbon, Portugal"

    def test_keywords_are_included(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place()]))])
        make_provider(transport).search(
            SearchQuery(city="Lisbon", business_type="restaurants", keywords=["vegan"], limit=5)
        )
        assert transport.requests[0]["body"]["textQuery"] == "restaurants vegan in Lisbon"

    def test_omits_location_when_not_supplied(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place()]))])
        make_provider(transport).search(SearchQuery(business_type="pharmacies", limit=5))
        assert transport.requests[0]["body"]["textQuery"] == "pharmacies"

    def test_sends_key_in_header_only(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place()]))])
        make_provider(transport, api_key="fake-key").search(SearchQuery(city="Aden", limit=5))

        headers = transport.requests[0]["headers"]
        assert headers["X-Goog-Api-Key"] == "fake-key"
        assert headers["X-Goog-FieldMask"] == DEFAULT_FIELD_MASK
        # The key must not be smuggled into the request payload as well.
        assert "fake-key" not in json.dumps(transport.requests[0]["body"])

    def test_no_pii_or_unrequested_fields_in_field_mask(self) -> None:
        # The mask is the contract for "public business data only".
        assert "places.displayName" in DEFAULT_FIELD_MASK
        assert "places.websiteUri" in DEFAULT_FIELD_MASK
        for forbidden in ("email", "personal", "reviews.text"):
            assert forbidden not in DEFAULT_FIELD_MASK


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


class TestPagination:
    def test_follows_next_page_token(self) -> None:
        transport = RecordingTransport(
            [
                (200, google_page([google_place("a", "A")], next_page_token="TOKEN-1")),
                (200, google_page([google_place("b", "B")])),
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert response.count == 2
        assert response.pages_fetched == 2
        assert "pageToken" not in transport.requests[0]["body"]
        assert transport.requests[1]["body"]["pageToken"] == "TOKEN-1"

    def test_stops_at_max_pages_even_with_more_tokens(self) -> None:
        transport = RecordingTransport(
            [
                (200, google_page([google_place("a", "A")], next_page_token="T1")),
                (200, google_page([google_place("b", "B")], next_page_token="T2")),
                (200, google_page([google_place("c", "C")], next_page_token="T3")),
            ]
        )
        provider = make_provider(transport, max_pages=2)
        response = provider.search(SearchQuery(city="Aden", limit=50))

        assert response.pages_fetched == 2
        assert response.count == 2
        assert len(transport.requests) == 2

    def test_does_not_request_a_second_page_when_limit_met(self) -> None:
        transport = RecordingTransport(
            [
                (200, google_page([google_place("a", "A"), google_place("b", "B")], "T1")),
                (200, google_page([google_place("c", "C")])),
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=2))

        assert response.count == 2
        assert response.pages_fetched == 1
        assert len(transport.requests) == 1

    def test_page_size_never_exceeds_the_api_maximum(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place()]))])
        make_provider(transport, page_size=100).search(SearchQuery(city="Aden", limit=100))

        assert transport.requests[0]["body"]["pageSize"] == MAX_PAGE_SIZE

    def test_second_page_requests_only_what_is_still_needed(self) -> None:
        transport = RecordingTransport(
            [
                (200, google_page([google_place("a", "A")], next_page_token="T1")),
                (200, google_page([google_place("b", "B")])),
            ]
        )
        # limit 4: page one asks 4, leaving 3; the second page asks 3.
        make_provider(transport).search(SearchQuery(city="Aden", limit=4))

        assert transport.requests[0]["body"]["pageSize"] == 4
        assert transport.requests[1]["body"]["pageSize"] == 3


# --------------------------------------------------------------------------- #
# Result limits
# --------------------------------------------------------------------------- #


class TestResultLimits:
    def test_never_returns_more_than_requested(self) -> None:
        places = [google_place(f"id-{index}", f"Business {index}") for index in range(15)]
        transport = RecordingTransport([(200, google_page(places))])

        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert response.count == 5
        assert [lead["source_id"] for lead in response.leads] == [
            "id-0",
            "id-1",
            "id-2",
            "id-3",
            "id-4",
        ]

    def test_limit_of_one(self) -> None:
        places = [google_place(f"id-{index}") for index in range(10)]
        transport = RecordingTransport([(200, google_page(places))])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=1))
        assert response.count == 1

    def test_caps_across_pages(self) -> None:
        """A full first page must not overflow the limit via page two."""
        transport = RecordingTransport(
            [
                (200, google_page([google_place(f"p1-{i}") for i in range(3)], "T1")),
                (200, google_page([google_place(f"p2-{i}") for i in range(3)])),
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=4))

        assert response.count == 4
        assert len(response.leads) == 4


# --------------------------------------------------------------------------- #
# Empty results
# --------------------------------------------------------------------------- #


class TestEmptyResults:
    def test_empty_places_list_is_a_success(self) -> None:
        transport = RecordingTransport([(200, {"places": []})])
        response = make_provider(transport).search(SearchQuery(city="Nowhere", limit=10))

        assert response.ok
        assert response.error is None
        assert response.count == 0
        assert response.empty is True

    def test_missing_places_key_is_treated_as_empty(self) -> None:
        transport = RecordingTransport([(200, {})])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert response.ok
        assert response.empty is True

    def test_unnamed_places_are_dropped_not_errors(self) -> None:
        transport = RecordingTransport(
            [
                (
                    200,
                    google_page(
                        [
                            google_place("keep", "Named Shop"),
                            {"id": "drop", "displayName": {"text": "   "}},
                            {"id": "drop2"},
                        ]
                    ),
                )
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert response.ok
        assert [lead["source_id"] for lead in response.leads] == ["keep"]


# --------------------------------------------------------------------------- #
# Malformed responses
# --------------------------------------------------------------------------- #


class TestMalformedResponses:
    def test_places_not_a_list_is_reported(self) -> None:
        transport = RecordingTransport([(200, {"places": "not-a-list"})])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.MALFORMED_RESPONSE

    def test_invalid_json_is_reported(self) -> None:
        transport = RecordingTransport([(200, "{not valid json")])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.MALFORMED_RESPONSE

    def test_top_level_list_is_reported(self) -> None:
        transport = RecordingTransport([(200, [1, 2, 3])])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.MALFORMED_RESPONSE

    def test_non_object_entries_are_skipped_without_losing_the_page(self) -> None:
        transport = RecordingTransport(
            [(200, {"places": ["junk", 42, None, google_place("good", "Good Shop")]})]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert response.ok
        assert [lead["source_id"] for lead in response.leads] == ["good"]

    def test_odd_field_types_do_not_break_mapping(self) -> None:
        """Numeric ratings arriving as strings, missing location, and so on."""
        transport = RecordingTransport(
            [
                (
                    200,
                    google_page(
                        [
                            {
                                "id": "odd",
                                "displayName": "Plain String Name",
                                "rating": "4.2",
                                "userRatingCount": "17",
                                "location": {"latitude": "12.5", "longitude": None},
                                "addressComponents": "not-a-list",
                                "types": None,
                            }
                        ]
                    ),
                )
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert response.ok
        record = response.leads[0]
        assert record["business_name"] == "Plain String Name"
        assert record["rating"] == 4.2
        assert record["review_count"] == 17
        assert record["latitude"] == 12.5
        assert "longitude" not in record


# --------------------------------------------------------------------------- #
# API errors
# --------------------------------------------------------------------------- #


class TestApiErrors:
    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_auth_and_config_failures_are_classified(self, status: int) -> None:
        transport = RecordingTransport([(status, {"error": {"message": "denied"}})])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.INVALID_CONFIG

    def test_server_error_is_reported_as_provider_error(self) -> None:
        transport = RecordingTransport([(500, "internal error")])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.PROVIDER_ERROR

    def test_transport_failure_is_a_network_error(self) -> None:
        def failing(method, url, params, data, headers, timeout):
            return HttpResponse(0, "", {}, url, error="connection reset")

        provider = GooglePlacesProvider(
            {"api_key": "fake-key", "client": HttpClient(transport=failing)}
        )
        response = provider.search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.NETWORK

    def test_error_message_never_contains_the_key(self) -> None:
        secret = "super-secret-value-12345"
        transport = RecordingTransport([(403, {"error": {"message": "API key not valid"}})])
        response = make_provider(transport, api_key=secret).search(
            SearchQuery(city="Aden", limit=5)
        )

        assert response.error is not None
        assert secret not in response.error
        # Guard the whole outgoing request record, not just the message: the key
        # belongs in the auth header and nowhere else.
        assert secret not in json.dumps(transport.requests[0]["body"])


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #


class TestRateLimits:
    def test_http_429_is_a_rate_limit(self) -> None:
        transport = RecordingTransport([(429, {"error": {"message": "quota exceeded"}})])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))

        assert not response.ok
        assert response.error_kind == ProviderErrorKind.RATE_LIMITED

    def test_rate_limit_is_marked_retryable(self) -> None:
        from lead_finder_agent.search.base import ProviderError

        error = ProviderError("too many requests", kind=ProviderErrorKind.RATE_LIMITED)
        assert error.retryable is True
        assert ProviderError("bad key", kind=ProviderErrorKind.INVALID_CONFIG).retryable is False

    def test_rate_limit_message_guides_the_operator(self) -> None:
        transport = RecordingTransport([(429, {})])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=5))
        assert "retry later" in response.error.lower()


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


class TestDeduplication:
    def test_duplicate_ids_within_one_page_collapse(self) -> None:
        transport = RecordingTransport(
            [
                (
                    200,
                    google_page(
                        [
                            google_place("same", "First"),
                            google_place("same", "First Duplicate"),
                            google_place("other", "Different"),
                        ]
                    ),
                )
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert [lead["source_id"] for lead in response.leads] == ["same", "other"]
        assert response.leads[0]["business_name"] == "First"

    def test_duplicate_ids_across_pages_collapse(self) -> None:
        transport = RecordingTransport(
            [
                (200, google_page([google_place("dup", "Shop")], next_page_token="T1")),
                (200, google_page([google_place("dup", "Shop Again")])),
            ]
        )
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert response.count == 1
        assert response.leads[0]["business_name"] == "Shop"

    def test_missing_ids_still_deduplicate_by_content(self) -> None:
        """Google always sends an id, but a missing one must not create repeats."""
        no_id = google_place("x", "Same Shop", city="Aden", country="Yemen")
        no_id.pop("id")
        transport = RecordingTransport([(200, google_page([no_id, dict(no_id)]))])
        response = make_provider(transport).search(SearchQuery(city="Aden", limit=10))

        assert response.count == 1


# --------------------------------------------------------------------------- #
# International locations (nothing is country-specific)
# --------------------------------------------------------------------------- #


class TestInternationalLocations:
    @pytest.mark.parametrize(
        ("city", "country"),
        [
            ("Aden", "Yemen"),
            ("Lisbon", "Portugal"),
            ("Tokyo", "Japan"),
            ("São Paulo", "Brazil"),
            ("Nairobi", "Kenya"),
        ],
    )
    def test_query_is_built_for_any_location(self, city: str, country: str) -> None:
        transport = RecordingTransport(
            [(200, google_page([google_place("a", "A", city=city, country=country)]))]
        )
        response = make_provider(transport).search(
            SearchQuery(city=city, country=country, business_type="restaurants", limit=5)
        )

        assert response.ok
        assert transport.requests[0]["body"]["textQuery"] == (
            f"restaurants in {city}, {country}"
        )
        # The record keeps whatever the source reported for that country.
        assert response.leads[0]["city"] == city
        assert response.leads[0]["country"] == country

    def test_records_use_source_location_not_the_search_term(self) -> None:
        """A business's own address wins over the area we searched."""
        transport = RecordingTransport(
            [
                (
                    200,
                    google_page([google_place("a", "A", city="Porto", country="Portugal")]),
                )
            ]
        )
        response = make_provider(transport).search(
            SearchQuery(city="Lisbon", country="Portugal", limit=5)
        )

        assert response.leads[0]["city"] == "Porto"
        assert response.leads[0]["country"] == "Portugal"

    def test_falls_back_to_search_area_when_source_omits_it(self) -> None:
        transport = RecordingTransport(
            [(200, google_page([google_place("a", "A", addressComponents=[])]))]
        )
        response = make_provider(transport).search(
            SearchQuery(city="Lisbon", country="Portugal", limit=5)
        )

        assert response.leads[0]["city"] == "Lisbon"
        assert response.leads[0]["country"] == "Portugal"

    def test_arabic_query_text_is_preserved(self) -> None:
        transport = RecordingTransport([(200, google_page([google_place("a", "مطعم عدن")]))])
        response = make_provider(transport).search(
            SearchQuery(city="عدن", country="اليمن", business_type="المطاعم", limit=5)
        )

        assert response.ok
        assert "المطاعم" in transport.requests[0]["body"]["textQuery"]
        assert response.leads[0]["business_name"] == "مطعم عدن"


# --------------------------------------------------------------------------- #
# Availability / configuration
# --------------------------------------------------------------------------- #


class TestAvailability:
    def test_unavailable_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        provider = GooglePlacesProvider({})

        assert provider.is_available() is False
        assert "GOOGLE_PLACES_API_KEY" in provider.unavailable_reason()

    def test_key_is_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "from-env")
        provider = GooglePlacesProvider({})

        assert provider.is_available() is True
        # Confirm it is used, without putting the value into an assertion
        # message that could leak into a log.
        transport = RecordingTransport([(200, google_page([]))])
        provider.client = transport.client()
        provider.search(SearchQuery(city="Aden", limit=5))
        assert transport.requests[0]["headers"]["X-Goog-Api-Key"] == "from-env"

    def test_key_env_name_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_OWN_PLACES_KEY", "custom-env")
        provider = GooglePlacesProvider({"api_key_env": "MY_OWN_PLACES_KEY"})

        assert provider.is_available() is True
        assert provider.requires_key_env == "GOOGLE_PLACES_API_KEY"

    def test_missing_key_produces_a_skip_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        response = GooglePlacesProvider({}).search(SearchQuery(city="Aden", limit=5))

        assert response.error is None
        assert response.skipped_reason is not None
        assert response.count == 0

    def test_language_hint_is_optional(self) -> None:
        transport = RecordingTransport([(200, google_page([]))])

        make_provider(transport).search(SearchQuery(city="Aden", limit=5))
        assert "languageCode" not in transport.requests[0]["body"]

        transport2 = RecordingTransport([(200, google_page([]))])
        make_provider(transport2, language_code="ar").search(SearchQuery(city="Aden", limit=5))
        assert transport2.requests[0]["body"]["languageCode"] == "ar"

    def test_endpoint_is_configurable(self) -> None:
        transport = RecordingTransport([(200, google_page([]))])
        make_provider(
            transport, google_places_endpoint="https://proxy.internal/places:searchText"
        ).search(SearchQuery(city="Aden", limit=5))

        assert transport.requests[0]["url"] == "https://proxy.internal/places:searchText"

    def test_is_registered_and_named(self) -> None:
        from lead_finder_agent.search.registry import available_providers

        assert GooglePlacesProvider.name == "google_places"
        assert "google_places" in available_providers()
