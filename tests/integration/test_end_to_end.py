"""Integration tests: whole-system behaviour across real components.

Unlike the unit tests these wire the real pipeline, CLI, storage engine and an
HTTP layer together. The only stubbed piece is the network transport, so the
suite still runs offline.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.cli import main
from lead_finder_agent.core import LeadFinderPipeline
from lead_finder_agent.models import SearchQuery, WebsiteStatus
from lead_finder_agent.search.providers.osm import OSMProvider
from lead_finder_agent.search.providers.sample import SampleProvider
from lead_finder_agent.storage import LeadFilter, SQLiteLeadRepository

from tests.conftest import FakeTransport

# The suite is offline by default. This marker lets the network-dependent tests
# be selected or excluded as a group, and keeps `-m "not integration"` honest.
pytestmark = pytest.mark.integration


@pytest.fixture
def osm_scenario(fake_transport: FakeTransport, nominatim_payload, osm_overpass_payload):
    """An OSM provider backed by canned Overpass/Nominatim responses."""
    fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
    fake_transport.add("overpass", body=json.dumps(osm_overpass_payload))
    return fake_transport


class TestEndToEndDiscovery:
    def test_osm_to_storage_flow(self, osm_scenario, fake_transport, good_page_html, tmp_path: Path):
        """Full path: OSM search -> normalize -> dedupe -> check -> score -> store."""
        # Only one business in the fixture has a website; give it a good page.
        fake_transport.add("facebook.com", status=200, body=good_page_html)

        repo = SQLiteLeadRepository(tmp_path / "e2e.db")
        pipeline = LeadFinderPipeline(
            providers=[OSMProvider({"client": fake_transport.client(), "default_country": "Yemen"})],
            checker=HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False),
            repository=repo,
        )
        result = pipeline.run(
            SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=20)
        )

        assert result.count == 2
        assert repo.count() == 2

        stored = repo.find(LeadFilter(city="Aden"))
        assert {lead.business_name for lead in stored} == {
            "Al Bahr Seafood Restaurant",
            "Golden Star Bakery",
        }

        # The scored, stored lead carries its reason trail for the sales team.
        top = repo.top(1)[0]
        assert top.lead_score > 0
        assert top.score_reason
        assert top.website_status in (WebsiteStatus.UNKNOWN, WebsiteStatus.NOT_FOUND)

        repo.close()

    def test_export_after_discovery_produces_usable_files(
        self, osm_scenario, fake_transport, big_good_html, tmp_path: Path
    ):
        fake_transport.add("facebook.com", status=200, body=big_good_html)
        db = tmp_path / "export.db"

        run = main(
            [
                "--db", str(db), "search",
                "--city", "Aden", "--country", "Yemen",
                "--providers", "sample",
                "--no-website-check",
                "--limit", "8",
            ]
        )
        assert run == 0

        csv_path = tmp_path / "leads.csv"
        json_path = tmp_path / "leads.json"
        assert main(["--db", str(db), "export", "--format", "csv", "--output", str(csv_path)]) == 0
        assert main(["--db", str(db), "export", "--format", "json", "--output", str(json_path)]) == 0

        rows = list(csv.DictReader(csv_path.read_text().splitlines()))
        assert len(rows) == 8
        assert rows[0]["business_name"]

        payload = json.loads(json_path.read_text())
        assert payload["metadata"]["count"] == 8
        # Leads are ordered best-first, which is what a sales workflow needs.
        scores = [lead["lead_score"] for lead in payload["leads"]]
        assert scores == sorted(scores, reverse=True)

    def test_repeated_runs_do_not_duplicate_data(self, tmp_path: Path):
        db = tmp_path / "repeat.db"
        args = [
            "--db", str(db), "search", "--city", "Aden",
            "--providers", "sample", "--no-website-check", "--limit", "8",
        ]
        assert main(args) == 0
        assert main(args) == 0

        repo = SQLiteLeadRepository(db)
        assert repo.count() == 8  # not 16
        repo.close()

    def test_repeated_run_keeps_improved_scores(self, tmp_path: Path):
        db = tmp_path / "improve.db"
        args = [
            "--db", str(db), "search", "--city", "Aden",
            "--providers", "sample", "--no-website-check", "--limit", "8",
        ]
        main(args)
        repo = SQLiteLeadRepository(db)
        first_scores = {lead.business_name: lead.lead_score for lead in repo.find()}
        repo.close()

        main(args)
        repo = SQLiteLeadRepository(db)
        second_scores = {lead.business_name: lead.lead_score for lead in repo.find()}
        repo.close()

        assert first_scores == second_scores


class TestResilience:
    def test_provider_outage_still_delivers_other_sources(self, fake_transport, nominatim_payload):
        """An Overpass outage must not empty the run when another source works."""
        fake_transport.add("nominatim", body=json.dumps(nominatim_payload))
        fake_transport.add("overpass", status=504, body="gateway timeout")

        repo = SQLiteLeadRepository(":memory:")
        pipeline = LeadFinderPipeline(
            providers=[
                OSMProvider({"client": fake_transport.client(), "default_country": "Yemen"}),
                SampleProvider(),
            ],
            repository=repo,
            check_websites=False,
        )
        result = pipeline.run(SearchQuery(city="Aden", country="Yemen", limit=20))

        assert result.count > 0
        assert result.errors or any(
            r.skipped_reason for r in result.search_result.responses
        )
        repo.close()

    def test_total_outage_returns_empty_result_without_crashing(self):
        def dead(method, url, params, data, headers, timeout):
            from lead_finder_agent.utils.http import HttpResponse

            return HttpResponse(0, "", {}, url, error="no route")

        repo = SQLiteLeadRepository(":memory:")
        result = LeadFinderPipeline(
            providers=[OSMProvider({"client": __import__(
                "lead_finder_agent.utils.http", fromlist=["HttpClient"]
            ).HttpClient(transport=dead)})],
            repository=repo,
            check_websites=False,
        ).run(SearchQuery(city="Aden", country="Yemen", limit=10))

        assert result.count == 0
        assert repo.count() == 0
        repo.close()

    def test_lead_with_broken_website_does_not_stop_the_run(self, fake_transport, good_page_html):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class _Provider(BaseSearchProvider):
            name = "mixed"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                return [
                    {"business_name": "Good Site Biz", "website_url": "https://good.example"},
                    {"business_name": "Broken Site Biz", "website_url": "https://bad.example"},
                    {"business_name": "No Site Biz"},
                ]

        fake_transport.add("good.example", status=200, body=good_page_html)
        # bad.example has no route -> treated as a network error.

        repo = SQLiteLeadRepository(":memory:")
        result = LeadFinderPipeline(
            providers=[_Provider()],
            checker=HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False),
            repository=repo,
        ).run(SearchQuery(city="Aden", limit=10))

        assert result.count == 3
        statuses = {lead.business_name: lead.website_status for lead in result.leads}
        assert statuses["Good Site Biz"] == WebsiteStatus.EXISTS
        assert statuses["Broken Site Biz"] == WebsiteStatus.UNREACHABLE
        assert statuses["No Site Biz"] == WebsiteStatus.UNKNOWN
        repo.close()


class TestScoringIntegration:
    def test_ordering_reflects_missing_website_opportunity(
        self, fake_transport, good_page_html
    ):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class _Provider(BaseSearchProvider):
            name = "compare"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                base = {
                    "city": "Aden",
                    "country": "Yemen",
                    "phone": "+967 71 000 0000",
                    "business_status": "active",
                    "rating": 4.5,
                    "review_count": 200,
                }
                return [
                    {**base, "business_name": "Has Great Website", "website_url": "https://great.example"},
                    {**base, "business_name": "Has No Website", "phone": "+967 71 000 0001"},
                ]

        fake_transport.add("great.example", status=200, body=good_page_html)
        repo = SQLiteLeadRepository(":memory:")
        result = LeadFinderPipeline(
            providers=[_Provider()],
            checker=HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False),
            repository=repo,
        ).run(SearchQuery(city="Aden", limit=10))

        assert result.leads[0].business_name == "Has No Website"
        assert result.leads[0].lead_score > result.leads[1].lead_score
        repo.close()

    def test_disqualified_business_is_filed_last(self, fake_transport):
        from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind

        class _Provider(BaseSearchProvider):
            name = "closed"
            kind = ProviderKind.CUSTOM

            def search_raw(self, query):
                return [
                    {
                        "business_name": "Still Open",
                        "city": "Aden",
                        "business_status": "active",
                        "phone": "+967 1",
                    },
                    {
                        "business_name": "Permanently Closed",
                        "city": "Aden",
                        "business_status": "closed",
                        "phone": "+967 2",
                    },
                ]

        repo = SQLiteLeadRepository(":memory:")
        result = LeadFinderPipeline(
            providers=[_Provider()], repository=repo, check_websites=False
        ).run(SearchQuery(city="Aden", limit=10))

        assert result.leads[0].business_name == "Still Open"
        assert str(result.leads[-1].priority) == "disqualified"
        repo.close()


@pytest.fixture
def big_good_html() -> str:
    return (
        "<html><head><title>Business</title></head><body>"
        + ("Quality products and services for every customer. " * 40)
        + '<a href="/contact">Contact</a><a href="/cart">Cart</a></body></html>'
    )
