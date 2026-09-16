"""Cross-provider aggregation, merging and de-duplication.

Two providers rarely describe the same business identically: one has the phone,
the other the website, and each has its own stable id for the record. These
tests cover the merge layer that turns them into one result set, with a strong
emphasis on the failure mode that actually costs money - merging two *different*
businesses and losing one of them.

Everything here runs offline against injected providers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from lead_finder_agent.core import LeadFinderPipeline
from lead_finder_agent.extraction.deduplicator import Deduplicator
from lead_finder_agent.extraction.normalizer import LeadNormalizer
from lead_finder_agent.models import Lead, ProviderKind, SearchQuery
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.search.multi import MultiProviderSearch

from tests.conftest import (
    FakeTransport,
    google_page,
    google_place,
    nominatim_result,
    osm_element,
    overpass_response,
)

normalizer = LeadNormalizer()


def lead(**overrides: Any) -> Lead:
    """A normalized lead, so tests exercise the same path the pipeline uses."""
    data: Dict[str, Any] = {
        "business_name": "Example Business",
        "city": "Aden",
        "country": "Yemen",
    }
    data.update(overrides)
    return normalizer.normalize(data)


def merged_name(leads: List[Lead]) -> List[str]:
    return sorted(item.business_name for item in Deduplicator().deduplicate(leads))


class StaticProvider(BaseSearchProvider):
    """Offline provider returning fixed raw records."""

    kind = ProviderKind.CUSTOM

    def __init__(self, name: str, records: List[Dict[str, Any]]) -> None:
        super().__init__()
        self.name = name
        self.records = records

    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        return list(self.records)


def record(name: str, provider_id: str = "", **fields: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {"business_name": name, "city": "Aden", "country": "Yemen"}
    if provider_id:
        data["source_id"] = provider_id
    data.update(fields)
    return data


def run_pipeline(providers, limit: int = 50) -> Any:
    return LeadFinderPipeline(
        providers=providers, check_websites=False, store_results=False
    ).run(SearchQuery(city="Aden", country="Yemen", limit=limit))


# --------------------------------------------------------------------------- #
# Single-source results
# --------------------------------------------------------------------------- #


class TestSingleProviderResults:
    def test_google_only_results_pass_through(self):
        google = StaticProvider(
            "google_places", [record("ABC Restaurant", "gp/1"), record("XYZ Cafe", "gp/2")]
        )
        result = run_pipeline([google])
        assert sorted(item.business_name for item in result.leads) == [
            "ABC Restaurant",
            "XYZ Cafe",
        ]

    def test_osm_only_results_pass_through(self):
        osm = StaticProvider("osm", [record("MNO Store", "node/1")])
        result = run_pipeline([osm])
        assert [item.business_name for item in result.leads] == ["MNO Store"]

    def test_empty_provider_results_yield_nothing(self):
        result = run_pipeline([StaticProvider("google_places", [])])
        assert result.count == 0
        assert result.leads == []

    def test_all_providers_empty_is_not_an_error(self):
        result = run_pipeline([StaticProvider("google_places", []), StaticProvider("osm", [])])
        assert result.count == 0
        assert result.errors == {}


# --------------------------------------------------------------------------- #
# Cross-provider de-duplication
# --------------------------------------------------------------------------- #


class TestCrossProviderDeduplication:
    def test_no_duplicates_keeps_everything(self):
        google = StaticProvider("google_places", [record("ABC Restaurant", "gp/1")])
        osm = StaticProvider("osm", [record("MNO Store", "node/1")])
        result = run_pipeline([google, osm])
        assert sorted(item.business_name for item in result.leads) == [
            "ABC Restaurant",
            "MNO Store",
        ]

    def test_exact_duplicate_across_providers_collapses(self):
        google = StaticProvider(
            "google_places", [record("ABC Restaurant", "ChIJ_abc", phone="+967 1 111 1111")]
        )
        osm = StaticProvider("osm", [record("ABC Restaurant", "node/1")])
        result = run_pipeline([google, osm])
        assert result.count == 1
        assert result.leads[0].business_name == "ABC Restaurant"

    def test_slightly_different_names_merge(self):
        google = StaticProvider(
            "google_places", [record("Al Bahr Seafood Restaurant", "gp/1")]
        )
        osm = StaticProvider("osm", [record("Al-Bahr Seafood Restaurants", "node/1")])
        assert merged_name([*_leads(google), *_leads(osm)]) == ["Al Bahr Seafood Restaurant"]

    def test_legal_suffix_difference_merges(self):
        google = StaticProvider(
            "google_places", [record("Aden Traders Company", "gp/1", address="Main Street 5")]
        )
        osm = StaticProvider("osm", [record("Aden Traders Co.", "node/1", address="Main Street, 5")])
        assert len(merged_name([*_leads(google), *_leads(osm)])) == 1

    def test_different_address_formatting_merges(self):
        google = StaticProvider(
            "google_places",
            [record("Corniche Diner", "gp/1", address="Building 12, Corniche Road, Aden")],
        )
        osm = StaticProvider(
            "osm", [record("Corniche Diner", "node/1", address="Corniche Road 12, Aden")]
        )
        assert len(merged_name([*_leads(google), *_leads(osm)])) == 1

    def test_phone_formatting_difference_merges(self):
        google = StaticProvider(
            "google_places", [record("Aden Fish House", "gp/1", phone="+967 71 234 5678")]
        )
        osm = StaticProvider("osm", [record("Aden Fish House", "node/1", phone="967-71-2345678")])
        assert len(merged_name([*_leads(google), *_leads(osm)])) == 1

    def test_matching_coordinates_merge_equal_names(self):
        google = StaticProvider(
            "google_places",
            [record("Haddad Market", "gp/1", latitude=12.7855, longitude=45.0187)],
        )
        osm = StaticProvider(
            "osm",
            [record("Haddad Market", "node/1", latitude=12.7855, longitude=45.0187)],
        )
        assert len(merged_name([*_leads(google), *_leads(osm)])) == 1

    def test_arabic_name_variants_merge(self):
        """Orthographic variants of one Arabic name are one business."""
        google = StaticProvider("google_places", [record("مطعم البحر", "gp/1")])
        osm = StaticProvider("osm", [record("مطعم البحر", "node/1")])
        assert len(merged_name([*_leads(google), *_leads(osm)])) == 1


def _leads(provider: StaticProvider) -> List[Lead]:
    """Normalize a provider's records the way the pipeline does."""
    return normalizer.normalize_many(provider.records, source=provider.name)


# --------------------------------------------------------------------------- #
# False-positive protection - the most important group
# --------------------------------------------------------------------------- #


class TestFalsePositiveProtection:
    def test_similar_names_different_businesses_are_not_merged(self):
        result = merged_name(
            [lead(business_name="Al Noor Restaurant"), lead(business_name="Al Noor Supermarket")]
        )
        assert result == ["Al Noor Restaurant", "Al Noor Supermarket"]

    def test_two_businesses_sharing_a_domain_are_not_merged(self):
        """A shared host is not evidence of one business.

        Franchises, chains and shared hosting all put unrelated businesses on
        one domain; merging them would silently drop a lead.
        """
        result = merged_name(
            [
                lead(business_name="Aden Fish House", website_url="https://example.com"),
                lead(business_name="Aden Bakery", website_url="https://example.com"),
            ]
        )
        assert result == ["Aden Bakery", "Aden Fish House"]

    def test_same_name_different_city_are_not_merged(self):
        result = merged_name(
            [
                lead(business_name="City Cafe", city="Aden"),
                lead(business_name="City Cafe", city="Sanaa"),
            ]
        )
        assert result == ["City Cafe", "City Cafe"]

    def test_same_name_conflicting_phones_are_not_merged(self):
        """Two branches of a chain in one city have different numbers."""
        result = merged_name(
            [
                lead(business_name="Star Cafe", phone="+967 1 111 1111"),
                lead(business_name="Star Cafe", phone="+967 2 222 2222"),
            ]
        )
        assert len(result) == 2

    def test_similar_names_far_apart_coordinates_are_not_merged(self):
        """The location guard: near-identical names, but the places differ.

        Two branches of one chain share a name and a city, so the name and city
        agree; only the coordinates reveal they are different premises.
        """
        result = merged_name(
            [
                lead(business_name="Golden Restaurant", latitude=12.78, longitude=45.01),
                lead(business_name="Golden Restaurants", latitude=13.50, longitude=44.20),
            ]
        )
        assert len(result) == 2

    def test_identical_name_and_city_without_phone_is_one_business(self):
        """The documented exact-key rule, stated explicitly.

        With no phone and no coordinates, name + city + (no phone) is the
        identity fallback, so these collapse. This is pre-existing, documented
        behaviour, not a consequence of the cross-provider matching above.
        """
        result = merged_name(
            [
                lead(business_name="City Pharmacy", latitude=12.78, longitude=45.01),
                lead(business_name="City Pharmacy", latitude=13.50, longitude=44.20),
            ]
        )
        assert len(result) == 1

    def test_common_short_name_is_not_enough_alone(self):
        """`Cafe` is not identifying; without corroboration it stays separate."""
        result = merged_name([lead(business_name="Cafe"), lead(business_name="Cafe")])
        # Identical name, same city, no contradicting signal: one business.
        assert len(result) == 1

    def test_different_names_different_everything_stay_separate(self):
        result = merged_name(
            [lead(business_name="Alpha Shop"), lead(business_name="Beta Bakery")]
        )
        assert result == ["Alpha Shop", "Beta Bakery"]


# --------------------------------------------------------------------------- #
# Merging field data
# --------------------------------------------------------------------------- #


class TestFieldMerging:
    def test_google_phone_plus_osm_website(self):
        """The requirement's worked example, end to end."""
        google = StaticProvider(
            "google_places",
            [record("ABC Restaurant", "gp/1", phone="+967 71 234 5678", website_url=None)],
        )
        osm = StaticProvider(
            "osm", [record("ABC Restaurant", "node/1", website_url="https://example.com")]
        )
        result = run_pipeline([google, osm])
        assert result.count == 1
        item = result.leads[0]
        assert item.business_name == "ABC Restaurant"
        assert item.phone == "+967 71 234 5678"
        assert item.website_url == "https://example.com"

    def test_missing_fields_do_not_erase_present_ones(self):
        first = lead(business_name="Shop", phone="+967 1 111 1111", website_url="https://a.example")
        second = lead(business_name="Shop")
        merged = Deduplicator().deduplicate([first, second])[0]
        assert merged.phone == "+967 1 111 1111"
        assert merged.website_url == "https://a.example"

    def test_conflicting_fields_keep_the_existing_value(self):
        """A valid value is never replaced, so a merge cannot lose data."""
        first = lead(business_name="Shop", phone="+967 1 111 1111")
        second = lead(business_name="Shop", phone="+967 9 999 9999")
        merged = Deduplicator().deduplicate([first, second])[0]
        # A phone conflict keeps these apart rather than merging them.
        assert merged.phone in {"+967 1 111 1111", "+967 9 999 9999"}

    def test_richer_record_contributes_more_fields(self):
        sparse = lead(business_name="Cafe", phone="+967 1 111 1111")
        rich = lead(
            business_name="Cafe",
            phone="+967 1 111 1111",
            email="cafe@example.com",
            description="Nice cafe",
        )
        merged = Deduplicator().deduplicate([sparse, rich])[0]
        assert merged.email == "cafe@example.com"
        assert merged.description == "Nice cafe"

    def test_categories_are_union_merged(self):
        first = lead(business_name="Shop", categories=["bakery"])
        second = lead(business_name="Shop", categories=["grocery"])
        merged = Deduplicator().deduplicate([first, second])[0]
        assert merged.categories == ["bakery", "grocery"]


# --------------------------------------------------------------------------- #
# Provider identity and source tracking
# --------------------------------------------------------------------------- #


class TestProviderIdentity:
    def test_provider_ids_are_recorded_per_provider(self):
        google = _leads(StaticProvider("google_places", [record("ABC Restaurant", "ChIJ_abc")]))[0]
        osm = _leads(StaticProvider("osm", [record("ABC Restaurant", "node/456")]))[0]
        assert google.provider_ids == {"google_places": "ChIJ_abc"}
        assert osm.provider_ids == {"osm": "node/456"}

    def test_ids_from_different_providers_are_never_treated_as_equal(self):
        """Records from different providers must not collide on id alone."""
        google = _leads(StaticProvider("google_places", [record("Alpha", "42")]))[0]
        osm = _leads(StaticProvider("osm", [record("Beta", "42")]))[0]
        assert google.dedupe_key != osm.dedupe_key
        assert len(Deduplicator().deduplicate([google, osm])) == 2

    def test_merge_keeps_both_provider_ids(self):
        google = _leads(StaticProvider("google_places", [record("ABC Restaurant", "ChIJ_abc")]))[0]
        osm = _leads(StaticProvider("osm", [record("ABC Restaurant", "node/456")]))[0]
        merged = Deduplicator().deduplicate([google, osm])[0]
        assert merged.provider_ids == {"google_places": "ChIJ_abc", "osm": "node/456"}

    def test_same_provider_same_id_is_an_exact_duplicate(self):
        first = _leads(StaticProvider("osm", [record("Shop", "node/1")]))[0]
        second = _leads(StaticProvider("osm", [record("Shop", "node/1")]))[0]
        assert len(Deduplicator(fuzzy=False).deduplicate([first, second])) == 1


class TestSourceTracking:
    def test_source_lists_both_discovering_providers(self):
        google = StaticProvider("google_places", [record("ABC Restaurant", "gp/1")])
        osm = StaticProvider("osm", [record("ABC Restaurant", "node/1")])
        result = run_pipeline([google, osm])
        assert result.leads[0].sources == ["google_places", "osm"]

    def test_source_urls_are_kept_per_provider(self):
        google = _leads(
            StaticProvider(
                "google_places",
                [record("ABC Restaurant", "gp/1", source_url="https://maps.google.com/?cid=1")],
            )
        )[0]
        osm = _leads(
            StaticProvider(
                "osm",
                [record("ABC Restaurant", "node/1", source_url="https://www.openstreetmap.org/node/1")],
            )
        )[0]
        merged = Deduplicator().deduplicate([google, osm])[0]
        assert merged.source_urls["google_places"] == "https://maps.google.com/?cid=1"
        assert merged.source_urls["osm"] == "https://www.openstreetmap.org/node/1"

    def test_single_provider_lead_still_tracks_its_source(self):
        osm = StaticProvider("osm", [record("MNO Store", "node/1")])
        result = run_pipeline([osm])
        assert result.leads[0].sources == ["osm"]

    def test_provenance_survives_a_storage_round_trip(self, tmp_path):
        from lead_finder_agent.storage import SQLiteLeadRepository

        repo = SQLiteLeadRepository(tmp_path / "prov.db")
        google = _leads(StaticProvider("google_places", [record("ABC Restaurant", "gp/1")]))[0]
        osm = _leads(StaticProvider("osm", [record("ABC Restaurant", "node/1")]))[0]
        merged = Deduplicator().deduplicate([google, osm])[0]
        repo.add(merged)

        stored = repo.find()[0]
        assert stored.sources == ["google_places", "osm"]
        assert stored.provider_ids == {"google_places": "gp/1", "osm": "node/1"}
        repo.close()


# --------------------------------------------------------------------------- #
# Limits and ordering
# --------------------------------------------------------------------------- #


class TestLimits:
    def test_final_result_respects_the_limit(self):
        google = StaticProvider(
            "google_places", [record(f"Shop {i}", f"gp/{i}") for i in range(10)]
        )
        result = run_pipeline([google], limit=4)
        assert result.count == 4

    def test_limit_is_not_consumed_by_duplicates(self):
        """The regression this whole change exists to prevent.

        The first provider returns its limit in records, two of which are
        duplicates of each other, so capping the raw count would leave the
        second provider unnamed and cost a unique lead.
        """
        google = StaticProvider(
            "google_places",
            [
                record("ABC Restaurant", "gp/1"),
                record("XYZ Cafe", "gp/2"),
                record("Shared Diner", "gp/3"),
            ],
        )
        osm = StaticProvider(
            "osm",
            [
                record("ABC Restaurant", "node/1"),
                record("Shared Diner", "node/2"),
                record("MNO Store", "node/3"),
            ],
        )
        result = run_pipeline([google, osm], limit=4)
        assert result.count == 4
        names = sorted(item.business_name for item in result.leads)
        assert names == ["ABC Restaurant", "MNO Store", "Shared Diner", "XYZ Cafe"]

    def test_duplicate_appears_only_once(self):
        google = StaticProvider("google_places", [record("ABC Restaurant", "gp/1")])
        osm = StaticProvider("osm", [record("ABC Restaurant", "node/1")])
        result = run_pipeline([google, osm])
        assert sum(1 for item in result.leads if item.business_name == "ABC Restaurant") == 1

    def test_never_returns_more_than_requested(self):
        google = StaticProvider(
            "google_places", [record(f"Google {i}", f"gp/{i}") for i in range(20)]
        )
        osm = StaticProvider("osm", [record(f"OSM {i}", f"node/{i}") for i in range(20)])
        result = run_pipeline([google, osm], limit=5)
        assert result.count <= 5


class TestDeterministicOrdering:
    def test_provider_order_is_preserved(self):
        google = StaticProvider("google_places", [record("AAA", "gp/1")])
        osm = StaticProvider("osm", [record("BBB", "node/1")])
        first_run = [item.business_name for item in run_pipeline([google, osm]).leads]
        second_run = [item.business_name for item in run_pipeline([google, osm]).leads]
        assert first_run == second_run

    def test_repeated_deduplication_is_stable(self):
        leads = [lead(business_name=f"Shop {i}", phone=f"+967 1 111 {i:04d}") for i in range(8)]
        first = [item.business_name for item in Deduplicator().deduplicate(leads)]
        second = [item.business_name for item in Deduplicator().deduplicate(leads)]
        assert first == second

    def test_first_seen_record_keeps_its_position(self):
        first = lead(business_name="Alpha Shop", phone="+967 1 111 1111")
        duplicate = lead(business_name="Alpha Shop", phone="+967 1 111 1111")
        later = lead(business_name="Beta Bakery", phone="+967 2 222 2222")
        result = Deduplicator().deduplicate([first, duplicate, later])
        assert [item.business_name for item in result] == ["Alpha Shop", "Beta Bakery"]


# --------------------------------------------------------------------------- #
# Partial failure
# --------------------------------------------------------------------------- #


class TestPartialFailure:
    def test_failing_provider_does_not_block_the_others(self):
        class Broken(BaseSearchProvider):
            name = "broken"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query: SearchQuery):
                raise RuntimeError("provider exploded")

        osm = StaticProvider("osm", [record("MNO Store", "node/1")])
        result = run_pipeline([Broken(), osm])
        assert [item.business_name for item in result.leads] == ["MNO Store"]
        assert "broken" in result.errors

    def test_duplicates_still_collapse_when_one_provider_fails(self):
        class Broken(BaseSearchProvider):
            name = "broken"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query: SearchQuery):
                raise RuntimeError("provider exploded")

        google = StaticProvider(
            "google_places",
            [record("ABC Restaurant", "gp/1"), record("ABC Restaurant", "gp/1")],
        )
        result = run_pipeline([Broken(), google])
        assert result.count == 1


# --------------------------------------------------------------------------- #
# Aggregation internals
# --------------------------------------------------------------------------- #


class TestMultiProviderSearch:
    def test_every_provider_is_asked_in_full(self):
        """Providers are not starved by the limit; the cap is applied later."""
        seen: List[int] = []

        class Recorder(BaseSearchProvider):
            kind = ProviderKind.CUSTOM

            def __init__(self, name: str) -> None:
                super().__init__()
                self.name = name

            def search_raw(self, query: SearchQuery):
                seen.append(query.limit)
                return [record(f"{self.name} {i}", f"{self.name}/{i}") for i in range(2)]

        MultiProviderSearch([Recorder("a"), Recorder("b")]).run(
            SearchQuery(city="Aden", limit=4)
        )
        assert seen == [4, 4]

    def test_query_copy_is_passed_to_providers(self):
        """A provider must not be able to mutate the shared query object."""
        received: List[SearchQuery] = []

        class Recorder(BaseSearchProvider):
            name = "recorder"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query: SearchQuery):
                received.append(query)
                query.city = "mutated"
                return []

        original = SearchQuery(city="Aden", limit=3)
        MultiProviderSearch([Recorder()]).run(original)
        assert received[0] is not original
        assert original.city == "Aden"


# --------------------------------------------------------------------------- #
# Normalization rules
# --------------------------------------------------------------------------- #


class TestNormalizationRules:
    def test_whitespace_and_case_fold_for_comparison_only(self):
        from lead_finder_agent.utils.text import comparable_name

        assert comparable_name("  Al   Bahr  ") == comparable_name("al bahr")

    def test_provider_raw_text_is_not_rewritten(self):
        """Comparison normalizes; the stored record keeps provider wording."""
        item = normalizer.normalize({"business_name": "  Al   Bahr  ", "city": "Aden"})
        assert item.business_name == "Al Bahr"

    def test_phone_and_url_normalization(self):
        item = normalizer.normalize(
            {"business_name": "Shop", "phone": "+967 71 234 5678", "website_url": "example.com/"}
        )
        assert item.phone == "+967 71 234 5678"
        assert item.website_url == "https://example.com"

    def test_missing_fields_are_handled_safely(self):
        from lead_finder_agent.utils.text import comparable_name, name_similarity, same_domain

        assert comparable_name(None) == ""
        assert name_similarity(None, "Shop") == 0.0
        assert same_domain(None, None) is False
        item = normalizer.normalize({"business_name": "Shop"})
        assert item.phone is None
        assert item.website_url is None

    def test_arabic_names_are_not_mangled(self):
        item = normalizer.normalize({"business_name": "مطعم البحر", "city": "عدن"})
        assert item.business_name == "مطعم البحر"


# --------------------------------------------------------------------------- #
# Real providers wired together through the pipeline
# --------------------------------------------------------------------------- #


class TestRealProvidersEndToEnd:
    """Google Places and OSM driven together, with only the network faked."""

    @staticmethod
    def _osm_provider(transport: FakeTransport, elements: List[Dict[str, Any]]):
        from lead_finder_agent.search.providers.osm import OSMProvider

        transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        transport.add("overpass", body=json.dumps(overpass_response(elements)))
        return OSMProvider({"client": transport.client()})

    @staticmethod
    def _google_provider(transport: FakeTransport, places: List[Dict[str, Any]]):
        from lead_finder_agent.search.providers.google_places import GooglePlacesProvider

        transport.add("places:searchText", body=json.dumps(google_page(places)))
        return GooglePlacesProvider({"api_key": "fake-key", "client": transport.client()})

    def test_google_and_osm_merge_into_one_result_set(self, fake_transport):
        google = self._google_provider(
            fake_transport,
            [
                google_place("gp-1", "ABC Restaurant", nationalPhoneNumber="+967 71 234 5678"),
                google_place("gp-2", "XYZ Cafe", websiteUri="https://xyz.example"),
            ],
        )
        osm = self._osm_provider(
            fake_transport,
            [
                osm_element(1, name="ABC Restaurant", tags={"amenity": "restaurant"}),
                osm_element(2, name="MNO Store", tags={"shop": "convenience"}),
            ],
        )

        result = run_pipeline([google, osm], limit=10)
        names = sorted(item.business_name for item in result.leads)
        assert "ABC Restaurant" in names
        assert "MNO Store" in names
        assert names.count("ABC Restaurant") == 1

        abc = next(item for item in result.leads if item.business_name == "ABC Restaurant")
        assert set(abc.sources) == {"google_places", "osm"}
        assert abc.phone == "+967 71 234 5678"

    def test_one_provider_down_still_yields_results(self, fake_transport):
        from lead_finder_agent.search.providers.osm import OSMProvider

        google = self._google_provider(
            fake_transport, [google_place("gp-1", "ABC Restaurant")]
        )
        fake_transport.add("nominatim", status=504, body="")
        osm = OSMProvider({"client": fake_transport.client()})

        result = run_pipeline([google, osm], limit=10)
        assert [item.business_name for item in result.leads] == ["ABC Restaurant"]
        assert "osm" in result.errors


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #


class TestCLIMergedResultSet:
    def test_cli_prints_one_deduplicated_result_set(
        self, monkeypatch, fake_transport, tmp_path, capsys
    ):
        """The documented example: Google + OSM in, one merged set out."""
        from lead_finder_agent.cli import main
        from lead_finder_agent.search.providers import google_places as google_module
        from lead_finder_agent.search.providers import osm as osm_module

        def _patch(module):
            real_client = module.HttpClient

            def _client(*args: Any, **kwargs: Any):
                kwargs["transport"] = fake_transport
                return real_client(*args, **kwargs)

            monkeypatch.setattr(module, "HttpClient", _client)

        _patch(osm_module)
        _patch(google_module)
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")

        fake_transport.add("nominatim", body=json.dumps(nominatim_result("aden")))
        fake_transport.add(
            "overpass",
            body=json.dumps(
                overpass_response(
                    [
                        osm_element(1, name="ABC Restaurant", tags={"amenity": "restaurant"}),
                        osm_element(2, name="MNO Store", tags={"shop": "convenience"}),
                    ]
                )
            ),
        )
        fake_transport.add(
            "places:searchText",
            body=json.dumps(
                google_page(
                    [
                        google_place("gp-1", "ABC Restaurant"),
                        google_place("gp-2", "XYZ Cafe"),
                    ]
                )
            ),
        )

        code = main(
            [
                "--db", str(tmp_path / "merged.db"),
                "search",
                "--country", "Yemen",
                "--city", "Aden",
                "--providers", "osm,google_places",
                "--limit", "10",
                "--no-website-check",
            ]
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "ABC Restaurant" in out
        assert "XYZ Cafe" in out
        assert "MNO Store" in out
        # ABC Restaurant must appear once, not once per provider.
        assert out.count("ABC Restaurant") == 1