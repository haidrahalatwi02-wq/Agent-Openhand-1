"""Tests for the search providers, registry and multi-provider fan-out."""

from __future__ import annotations

import json

import pytest

from lead_finder_agent.models import SearchQuery
from lead_finder_agent.search import (
    MultiProviderSearch,
    available_providers,
    build_providers,
    register_provider,
)
from lead_finder_agent.search.base import BaseSearchProvider, ProviderSkip
from lead_finder_agent.search.business_types import BusinessTypeResolver
from lead_finder_agent.search.providers.osm import OSMProvider
from lead_finder_agent.search.providers.sample import SampleProvider
from lead_finder_agent.search.registry import registry

from tests.conftest import FakeTransport


class TestRegistry:
    def test_builtin_providers_registered(self):
        names = available_providers()
        assert "osm" in names
        assert "sample" in names

    def test_build_providers_returns_instances(self):
        providers = build_providers(["sample"])
        assert len(providers) == 1
        assert isinstance(providers[0], SampleProvider)

    def test_unknown_provider_is_skipped_with_warning(self):
        assert build_providers(["does-not-exist"]) == []

    def test_custom_provider_can_be_registered(self):
        @register_provider(name="unit-test-provider")
        class _Custom(BaseSearchProvider):
            name = "unit-test-provider"

            def search_raw(self, query):
                return [{"business_name": "Custom Biz"}]

        assert "unit-test-provider" in available_providers()
        provider = registry().create("unit-test-provider")
        assert provider.search(SearchQuery(limit=1)).count == 1

    def test_registry_get_unknown_raises(self):
        with pytest.raises(KeyError):
            registry().get("nope")


class TestProviderIsolation:
    def test_provider_error_is_captured_not_raised(self):
        class _Broken(BaseSearchProvider):
            name = "broken"

            def search_raw(self, query):
                raise RuntimeError("boom")

        response = _Broken().search(SearchQuery(limit=1))
        assert response.ok is False
        assert "boom" in response.error
        assert response.count == 0

    def test_provider_skip_is_reported_not_raised(self):
        class _Skipper(BaseSearchProvider):
            name = "skipper"

            def search_raw(self, query):
                raise ProviderSkip("needs a key")

        response = _Skipper().search(SearchQuery(limit=1))
        assert response.ok is True
        assert response.skipped_reason == "needs a key"

    def test_provider_requiring_missing_key_is_skipped(self, monkeypatch):
        monkeypatch.delenv("SOME_ABSENT_KEY", raising=False)

        class _Keyed(BaseSearchProvider):
            name = "keyed"
            requires_key_env = "SOME_ABSENT_KEY"

            def search_raw(self, query):  # pragma: no cover - never called
                raise AssertionError("should not run")

        response = _Keyed().search(SearchQuery(limit=1))
        assert response.skipped_reason is not None


class TestBusinessTypeResolver:
    def test_resolves_english_alias(self):
        resolver = BusinessTypeResolver.load()
        tags = resolver.resolve("restaurants")
        assert "amenity=restaurant" in tags

    def test_resolves_arabic_alias(self):
        resolver = BusinessTypeResolver.load()
        tags = resolver.resolve("محلات ملابس")
        assert any("clothes" in tag for tag in tags)

    def test_unknown_type_uses_fallback(self):
        resolver = BusinessTypeResolver.load()
        assert resolver.resolve("spaceship repair") == ["shop=*"]

    def test_empty_query_uses_fallback(self):
        resolver = BusinessTypeResolver.load()
        assert resolver.resolve(None) == ["shop=*"]

    def test_keywords_are_considered(self):
        resolver = BusinessTypeResolver.load()
        tags = resolver.resolve(None, ["bakery"])
        assert any("bakery" in tag for tag in tags)

    def test_category_for(self):
        resolver = BusinessTypeResolver.load()
        assert resolver.category_for("car repair") == "car_repair"

    def test_load_from_custom_file_merges_with_defaults(self, tmp_path):
        path = tmp_path / "types.json"
        path.write_text(
            json.dumps(
                {
                    "categories": {
                        "space_shop": {"aliases": ["rocket shop"], "tags": ["shop=rocket"]}
                    }
                }
            )
        )
        resolver = BusinessTypeResolver.load(path)
        assert resolver.resolve("rocket shop") == ["shop=rocket"]
        # Built-in categories survive the merge.
        assert "amenity=restaurant" in resolver.resolve("restaurants")

    def test_load_from_missing_file_falls_back_to_defaults(self, tmp_path):
        resolver = BusinessTypeResolver.load(tmp_path / "nope.json")
        assert resolver.resolve("restaurants") == BusinessTypeResolver.load().resolve("restaurants")

    def test_osm_provider_honours_business_types_path(self, tmp_path):
        path = tmp_path / "types.json"
        path.write_text(
            json.dumps(
                {"categories": {"x": {"aliases": ["widgets"], "tags": ["shop=widgets"]}}}
            )
        )
        provider = OSMProvider({"business_types_path": path})
        assert provider.resolver.resolve("widgets") == ["shop=widgets"]


class TestSampleProvider:
    def test_returns_matching_records(self):
        provider = SampleProvider()
        leads = provider.search(SearchQuery(city="Aden", business_type="restaurant", limit=10))
        assert leads.count >= 1
        assert all("restaurant" in (lead["business_type"] or "") for lead in leads.leads)

    def test_unknown_city_returns_nothing(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Atlantis", limit=10)).count == 0

    def test_limit_is_respected(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Aden", limit=3)).count == 3

    def test_no_filter_returns_all_for_city(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Aden", limit=50)).count == 8

    def test_is_always_available(self):
        assert SampleProvider().is_available() is True

    def test_records_carry_a_source_id(self):
        provider = SampleProvider()
        response = provider.search(SearchQuery(city="Aden", limit=1))
        assert response.leads[0]["source_id"].startswith("sample/")

    def test_resolves_an_arabic_business_type(self):
        provider = SampleProvider()
        leads = provider.search(SearchQuery(city="Aden", business_type="محلات ملابس", limit=10))
        assert leads.count >= 1
        assert all(lead["business_type"] == "clothing" for lead in leads.leads)

    def test_honours_a_custom_business_types_path(self, tmp_path):
        path = tmp_path / "types.json"
        path.write_text(
            json.dumps(
                {"categories": {"widget": {"aliases": ["widget"], "tags": ["shop=widget"]}}}
            )
        )
        provider = SampleProvider(
            {"business_types_path": path, "data": {"aden": [{"business_name": "W", "business_type": "widget"}]}}
        )
        assert provider.search(SearchQuery(city="Aden", business_type="widget", limit=5)).count == 1


class TestOSMProvider:
    def _provider(self, transport: FakeTransport) -> OSMProvider:
        return OSMProvider({"client": transport.client(), "default_country": "Yemen"})

    def test_geocodes_then_queries_overpass(
        self, fake_transport: FakeTransport, nominatim_payload, osm_overpass_payload
    ):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps(osm_overpass_payload))
        provider = self._provider(fake_transport)

        leads = provider.search(SearchQuery(city="Aden", country="Yemen", limit=10))
        assert leads.ok
        assert leads.count == 2  # the unnamed node is dropped

        names = [lead["business_name"] for lead in leads.leads]
        assert "Al Bahr Seafood Restaurant" in names
        assert "Golden Star Bakery" in names

    def test_social_website_moved_to_social_links(
        self, fake_transport: FakeTransport, nominatim_payload, osm_overpass_payload
    ):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps(osm_overpass_payload))
        provider = self._provider(fake_transport)

        leads = provider.search(SearchQuery(city="Aden", country="Yemen", limit=10))
        bahar = next(l for l in leads.leads if "Bahr" in l["business_name"])
        assert bahar["website_url"] is None
        assert "facebook" in bahar["social_links"]

    def test_overpass_query_contains_alias_tags_and_bbox(
        self, fake_transport: FakeTransport, nominatim_payload
    ):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps({"elements": []}))
        provider = self._provider(fake_transport)
        provider.search(SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=5))

        overpass_call = next(r for r in fake_transport.requests if "overpass" in r["url"])
        statement = overpass_call["data"]["data"]
        assert '["amenity"="restaurant"]' in statement
        assert "12.7" in statement and "45.1" in statement
        assert "out center tags 5" in statement

    def test_geocoding_failure_reports_skip(self, fake_transport: FakeTransport):
        fake_transport.add("nominatim", status=500, body="")
        provider = self._provider(fake_transport)
        response = provider.search(SearchQuery(city="Nowhere", limit=5))
        assert response.skipped_reason is not None
        assert response.count == 0

    def test_overpass_failure_reports_skip(
        self, fake_transport: FakeTransport, nominatim_payload
    ):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", status=504, body="")
        provider = self._provider(fake_transport)
        response = provider.search(SearchQuery(city="Aden", limit=5))
        assert response.skipped_reason is not None

    def test_invalid_json_reports_skip(self, fake_transport: FakeTransport, nominatim_payload):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body="{not json")
        response = self._provider(fake_transport).search(SearchQuery(city="Aden", limit=5))
        assert response.skipped_reason is not None

    def test_no_location_is_skipped(self, fake_transport: FakeTransport):
        provider = OSMProvider({"client": fake_transport.client()})
        response = provider.search(SearchQuery(limit=5))
        assert response.skipped_reason is not None

    def test_source_url_points_at_openstreetmap(
        self, fake_transport: FakeTransport, nominatim_payload, osm_overpass_payload
    ):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps(osm_overpass_payload))
        leads = self._provider(fake_transport).search(SearchQuery(city="Aden", limit=5))
        assert leads.leads[0]["source_url"].startswith("https://www.openstreetmap.org/node/")


class TestMultiProviderSearch:
    def test_aggregates_results_from_all_providers(self, fake_transport, nominatim_payload, osm_overpass_payload):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps(osm_overpass_payload))
        osm = OSMProvider({"client": fake_transport.client()})
        search = MultiProviderSearch([osm, SampleProvider()])

        result = search.run(SearchQuery(city="Aden", country="Yemen", limit=50))
        assert len(result.leads) >= 2
        assert "osm" in result.providers_used
        assert "sample" in result.providers_used

    def test_one_failing_provider_does_not_stop_others(self, fake_transport):
        fake_transport.add("nominatim", status=500, body="")
        osm = OSMProvider({"client": fake_transport.client()})
        result = MultiProviderSearch([osm, SampleProvider()]).run(
            SearchQuery(city="Aden", limit=10)
        )
        assert result.leads  # sample still delivered
        assert result.responses[0].skipped_reason is not None
        assert result.responses[1].ok

    def test_provider_errors_are_reported(self):
        class _Broken(BaseSearchProvider):
            name = "broken"

            def search_raw(self, query):
                raise RuntimeError("nope")

        result = MultiProviderSearch([_Broken()]).run(SearchQuery(limit=5))
        assert "broken" in result.errors
        assert result.leads == []

    def test_respects_limit(self):
        result = MultiProviderSearch([SampleProvider()]).run(SearchQuery(city="Aden", limit=2))
        assert len(result.leads) <= 2

    def test_empty_provider_list(self):
        result = MultiProviderSearch([]).run(SearchQuery(limit=5))
        assert result.leads == []
        assert result.responses == []