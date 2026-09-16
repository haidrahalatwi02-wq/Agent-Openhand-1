"""Integration tests: search provider -> normalize -> check -> CLI.

These exercise the real provider code paths (Google Places and OpenStreetMap)
against faked HTTP, so the website URL that a provider supplies is proven to
reach the checker intact. No test here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from lead_finder_agent.checker import HttpWebsiteChecker
from lead_finder_agent.core.pipeline import LeadFinderPipeline
from lead_finder_agent.models import SearchQuery, WebsiteStatus
from lead_finder_agent.search.providers.google_places import GooglePlacesProvider
from lead_finder_agent.search.providers.osm import OSMProvider
from lead_finder_agent.storage.sqlite_repository import SQLiteLeadRepository
from lead_finder_agent.utils.http import HttpResponse
from tests.conftest import FakeTransport, google_page, google_place

GOOD_HTML = (
    "<html><head><title>Aden Traders</title></head><body>"
    + ("Welcome to Aden Traders. " * 30)
    + "</body></html>"
)


def _checker(transport: FakeTransport) -> HttpWebsiteChecker:
    return HttpWebsiteChecker(client=transport.client(), probe_by_name=False)


class TestGooglePlacesWebsiteIntegration:
    def test_google_website_url_reaches_the_checker(self, fake_transport, google_places_key):
        fake_transport.add("places.googleapis.com", status=200, body=json.dumps(
            google_page([google_place("place-1", displayName="Aden Traders",
                                      websiteUri="https://adentraders.example")])
        ))
        fake_transport.add("adentraders.example", status=200, body=GOOD_HTML)

        provider = GooglePlacesProvider({"client": fake_transport.client()})
        repo = SQLiteLeadRepository(":memory:")
        result = LeadFinderPipeline(
            providers=[provider],
            checker=_checker(fake_transport),
            repository=repo,
        ).run(SearchQuery(city="Aden", country="Yemen", limit=5))

        assert result.count == 1
        lead = result.leads[0]
        assert lead.website_url == "https://adentraders.example"
        assert lead.website_status == WebsiteStatus.EXISTS
        # Provenance survives the whole pipeline.
        assert lead.raw["website_check"]["website_source"] == "google_places"
        repo.close()

    def test_google_business_with_no_website_is_unknown_not_not_found(
        self, fake_transport, google_places_key
    ):
        fake_transport.add("places.googleapis.com", status=200, body=json.dumps(
            google_page([google_place("place-2", displayName="No Site Cafe", websiteUri=None)])
        ))

        provider = GooglePlacesProvider({"client": fake_transport.client()})
        result = LeadFinderPipeline(
            providers=[provider],
            checker=_checker(fake_transport),
            store_results=False,
        ).run(SearchQuery(city="Aden", limit=5))

        assert result.count == 1
        assert result.leads[0].website_status == WebsiteStatus.UNKNOWN
        assert result.leads[0].website_status != WebsiteStatus.NOT_FOUND


class TestOsmWebsiteIntegration:
    def test_osm_website_tag_reaches_the_checker(self, fake_transport, nominatim_payload):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps({
            "elements": [
                {
                    "type": "node", "id": 501, "lat": 12.78, "lon": 45.01,
                    "tags": {"name": "Aden Bistro", "amenity": "restaurant",
                             "website": "https://adenbistro.example"},
                }
            ]
        }))
        fake_transport.add("adenbistro.example", status=200, body=GOOD_HTML)

        provider = OSMProvider({"client": fake_transport.client(), "default_country": "Yemen"})
        result = LeadFinderPipeline(
            providers=[provider],
            checker=_checker(fake_transport),
            store_results=False,
        ).run(SearchQuery(city="Aden", country="Yemen", limit=5))

        assert result.count == 1
        lead = result.leads[0]
        assert lead.website_url == "https://adenbistro.example"
        assert lead.website_status == WebsiteStatus.EXISTS
        assert lead.raw["website_check"]["website_source"] == "osm"

    def test_osm_business_without_website_tag_is_unknown(self, fake_transport, nominatim_payload):
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", body=json.dumps({
            "elements": [
                {
                    "type": "node", "id": 502, "lat": 12.78, "lon": 45.01,
                    "tags": {"name": "Untagged Shop", "shop": "bakery"},
                }
            ]
        }))

        provider = OSMProvider({"client": fake_transport.client(), "default_country": "Yemen"})
        result = LeadFinderPipeline(
            providers=[provider],
            checker=_checker(fake_transport),
            store_results=False,
        ).run(SearchQuery(city="Aden", country="Yemen", limit=5))

        assert result.count == 1
        # A missing `website` tag is not proof the business has no website.
        assert result.leads[0].website_status == WebsiteStatus.UNKNOWN


class TestPipelineWebsiteStages:
    def test_unreachable_website_does_not_stop_the_run(self, fake_transport):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class Provider(BaseSearchProvider):
            name = "mixed"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                return [
                    {"business_name": "Good Biz", "website_url": "https://good.example"},
                    {"business_name": "Broken Biz", "website_url": "https://broken.example"},
                    {"business_name": "Absent Biz"},
                ]

        fake_transport.add("good.example", status=200, body=GOOD_HTML)
        fake_transport.add_handler(
            "broken.example",
            lambda m, u, p, d, h, t: HttpResponse(0, "", {}, u, error="connection timed out"),
        )

        result = LeadFinderPipeline(
            providers=[Provider()],
            checker=_checker(fake_transport),
            store_results=False,
        ).run(SearchQuery(city="Aden", limit=10))

        statuses = {lead.business_name: lead.website_status for lead in result.leads}
        assert result.count == 3
        assert statuses["Good Biz"] == WebsiteStatus.EXISTS
        assert statuses["Broken Biz"] == WebsiteStatus.UNREACHABLE
        assert statuses["Absent Biz"] == WebsiteStatus.UNKNOWN

    def test_check_is_cached_across_duplicate_urls_in_one_run(self, fake_transport):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class Provider(BaseSearchProvider):
            name = "dupes"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                # Different names, same website: two businesses sharing a domain.
                return [
                    {"business_name": "Shop One", "website_url": "https://shared.example"},
                    {"business_name": "Shop Two", "website_url": "https://shared.example"},
                ]

        fake_transport.add("shared.example", status=200, body=GOOD_HTML)
        checker = _checker(fake_transport)
        result = LeadFinderPipeline(
            providers=[Provider()], checker=checker, store_results=False
        ).run(SearchQuery(city="Aden", limit=10))

        assert result.count == 2
        assert all(lead.website_status == WebsiteStatus.EXISTS for lead in result.leads)
        hits = [r for r in fake_transport.requests if "shared.example" in r["url"]]
        assert len(hits) == 1

    def test_website_check_stage_can_be_skipped(self, fake_transport):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class Provider(BaseSearchProvider):
            name = "skip"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                return [{"business_name": "A Biz", "website_url": "https://a.example"}]

        result = LeadFinderPipeline(
            providers=[Provider()],
            checker=_checker(fake_transport),
            check_websites=False,
            store_results=False,
        ).run(SearchQuery(city="Aden", limit=5))

        assert "website_check:skipped" in result.stats.failed_stages
        assert fake_transport.requests == []


class TestCliWebsiteStatus:
    def test_search_output_never_implies_missing_site(self, fake_transport, tmp_path, monkeypatch, capsys):
        from lead_finder_agent.cli import main

        fake_transport.add("places:searchText", body=json.dumps(
            google_page([
                google_place("p1", displayName="Has Site", websiteUri="https://has.example"),
                google_place("p2", displayName="No Site", websiteUri=None),
            ])
        ))
        fake_transport.add("has.example", status=200, body=GOOD_HTML)
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key-not-a-real-secret")
        monkeypatch.setenv("LEAD_FINDER_PROVIDERS", "google_places")

        # Patch the outbound clients so the real CLI path runs offline.
        from lead_finder_agent.search.providers import google_places as gp_module
        from lead_finder_agent.checker import http_checker as hc_module

        original_provider_init = gp_module.GooglePlacesProvider.__init__

        def patched_provider_init(self, config=None):
            config = dict(config or {})
            config["client"] = fake_transport.client()
            original_provider_init(self, config)

        original_checker_init = hc_module.HttpWebsiteChecker.__init__

        def patched_checker_init(self, config=None, **kwargs):
            kwargs["client"] = fake_transport.client()
            kwargs["probe_by_name"] = False
            original_checker_init(self, config=config, **kwargs)

        monkeypatch.setattr(gp_module.GooglePlacesProvider, "__init__", patched_provider_init)
        monkeypatch.setattr(hc_module.HttpWebsiteChecker, "__init__", patched_checker_init)

        code = main(
            [
                "--db", str(tmp_path / "cli.db"), "search",
                "--country", "Yemen", "--city", "Aden", "--limit", "5", "--no-store",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Website" in out
        # The table must never imply that an unconfirmed site means "no website".
        assert "does NOT mean the business has no website" in out