"""Tests for location resolution and category matching.

These cover the boundary where free-text search input is folded onto canonical
names. Nothing here touches the network: the resolver reads packaged data and
the provider uses the offline ``sample`` records.
"""

from __future__ import annotations

import json

import pytest

from lead_finder_agent.config.locations import LocationResolver
from lead_finder_agent.models import SearchQuery
from lead_finder_agent.models.search import reset_location_resolver
from lead_finder_agent.search.business_types import BusinessTypeResolver, categories_match
from lead_finder_agent.search.providers.sample import SampleProvider


class TestLocationResolver:
    def test_resolves_arabic_city_to_canonical_english(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_city("عدن")[0] == "Aden"

    def test_resolves_city_with_مدينة_qualifier(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_city("مدينة عدن")[0] == "Aden"

    def test_infers_country_from_city(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_city("عدن")[1] == "Yemen"

    def test_case_and_punctuation_are_ignored(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_city("ADEN")[0] == "Aden"
        assert resolver.resolve_city("Sana'a")[0] == "Sanaa"

    def test_unknown_city_passes_through_unchanged(self):
        """An unknown name must never be guessed at or dropped."""
        resolver = LocationResolver.load()
        city, country = resolver.resolve_city("Atlantis")
        assert city == "Atlantis"
        assert country is None

    def test_unknown_country_passes_through_unchanged(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_country("Narnia") == "Narnia"

    def test_arabic_country_resolves(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_country("اليمن") == "Yemen"

    def test_blank_input_returns_none(self):
        resolver = LocationResolver.load()
        assert resolver.resolve_city("   ") == (None, None)
        assert resolver.resolve_country("") is None

    def test_load_merges_an_override(self):
        resolver = LocationResolver.load(
            {"cities": {"Testville": {"country": "Testland", "aliases": ["testville"]}}}
        )
        assert resolver.resolve_city("Testville") == ("Testville", "Testland")
        # Packaged entries survive the merge.
        assert resolver.resolve_city("عدن")[0] == "Aden"

    def test_missing_data_file_degrades_to_pass_through(self, tmp_path, monkeypatch):
        """A broken data file must not break searching."""
        from lead_finder_agent.config import locations as locations_module

        def _boom(name):
            raise FileNotFoundError(name)

        monkeypatch.setattr(locations_module, "load_packaged_data", _boom)
        resolver = locations_module.LocationResolver.load()
        assert resolver.resolve_city("عدن") == ("عدن", None)


class TestSearchQueryLocationNormalization:
    def test_arabic_city_is_normalized_on_the_query(self):
        query = SearchQuery(city="عدن", limit=10)
        assert query.city == "Aden"
        assert query.country == "Yemen"

    def test_explicit_country_is_not_overridden(self):
        """A deliberate mismatch stays put so the filter can exclude it."""
        query = SearchQuery(city="Aden", country="Egypt", limit=10)
        assert query.city == "Aden"
        assert query.country == "Egypt"

    def test_arabic_country_is_normalized(self):
        query = SearchQuery(city="صنعاء", country="اليمن", limit=10)
        assert query.city == "Sanaa"
        assert query.country == "Yemen"

    def test_unknown_city_is_left_alone(self):
        query = SearchQuery(city="Atlantis", limit=10)
        assert query.city == "Atlantis"

    def test_normalization_can_be_reset(self):
        reset_location_resolver()
        assert SearchQuery(city="عدن", limit=1).city == "Aden"


class TestCategoriesMatch:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("restaurants", "restaurant"),
            ("restaurant", "restaurants"),
            ("clothing", "clothing"),
            ("RESTAURANTS", "restaurant"),
        ],
    )
    def test_matches_singular_and_plural(self, left, right):
        assert categories_match(left, right) is True

    @pytest.mark.parametrize("left,right", [("bakery", "clothing"), ("", "restaurant"), (None, None)])
    def test_rejects_different_or_empty_categories(self, left, right):
        assert categories_match(left, right) is False


class TestSampleProviderLocalizedSearch:
    def test_arabic_city_and_type_finds_the_matching_record(self):
        provider = SampleProvider()
        response = provider.search(SearchQuery(city="عدن", business_type="المطاعم", limit=20))
        assert response.count == 1
        assert response.leads[0]["business_name"] == "Al Bahr Seafood Restaurant"

    def test_singular_record_type_matches_plural_category(self):
        """Records store ``restaurant``; the resolver yields ``restaurants``."""
        provider = SampleProvider()
        response = provider.search(SearchQuery(city="Aden", business_type="المطاعم", limit=20))
        assert all(lead["business_type"] == "restaurant" for lead in response.leads)

    def test_arabic_type_only_returns_that_vertical(self):
        provider = SampleProvider()
        response = provider.search(SearchQuery(city="Aden", business_type="مخبز", limit=20))
        assert response.count == 1
        assert response.leads[0]["business_type"] == "bakery"

    def test_mismatched_country_excludes_records(self):
        """Aden is in Yemen, so asking for Egypt must return nothing."""
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Aden", country="Egypt", limit=10)).count == 0

    def test_matching_country_still_returns_records(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Aden", country="Yemen", limit=10)).count == 8

    def test_absent_country_does_not_filter(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Aden", limit=10)).count == 8

    def test_unknown_city_still_returns_nothing(self):
        provider = SampleProvider()
        assert provider.search(SearchQuery(city="Atlantis", limit=10)).count == 0


class TestLimitValidation:
    def test_explicit_zero_is_rejected_not_silently_defaulted(self):
        with pytest.raises(ValueError):
            SearchQuery(city="Aden", limit=0)

    def test_negative_limit_is_rejected(self):
        with pytest.raises(ValueError):
            SearchQuery(city="Aden", limit=-5)

    def test_agent_none_limit_falls_back_to_default(self, settings):
        """``None`` means "not supplied" and must still use the default."""
        from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent

        agent = LeadFinderAgent(
            context=AgentContext(settings=settings, providers=[SampleProvider()]),
            check_websites=False,
            store_results=False,
        )
        assert agent.run(city="Aden", limit=None).count > 0

    def test_agent_explicit_zero_limit_raises(self, settings):
        from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent

        agent = LeadFinderAgent(
            context=AgentContext(settings=settings, providers=[SampleProvider()]),
            check_websites=False,
            store_results=False,
        )
        with pytest.raises(ValueError):
            agent.run(city="Aden", limit=0)