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


class TestLocalizedCliWorkflow:
    """The documented workflow, driven by Arabic input as real users type it.

    ``lead-finder search --city عدن --type المطاعم --limit 20`` must behave
    exactly like its English equivalent: resolve the city, honour the country,
    persist, and export. Only the network is stubbed.
    """

    def _search(self, db_path: Path, *extra: str) -> int:
        return main(
            [
                "--db", str(db_path), "search",
                "--city", "عدن",
                "--type", "المطاعم",
                "--providers", "sample",
                "--no-website-check",
                "--limit", "20",
                *extra,
            ]
        )

    def test_arabic_search_reaches_storage(self, tmp_path: Path, capsys):
        db_path = tmp_path / "ar.db"
        assert self._search(db_path) == 0
        capsys.readouterr()

        repo = SQLiteLeadRepository(db_path)
        stored = repo.find(LeadFilter(city="Aden"))
        assert len(stored) == 1
        assert stored[0].business_name == "Al Bahr Seafood Restaurant"
        # The Arabic city was folded onto its canonical spelling before storage.
        assert stored[0].country == "Yemen"
        repo.close()

    def test_arabic_and_english_searches_agree(self, tmp_path: Path, capsys):
        """The localized run must produce the same lead set as the English one."""
        arabic_db = tmp_path / "ar.db"
        english_db = tmp_path / "en.db"
        self._search(arabic_db)
        capsys.readouterr()
        main(
            [
                "--db", str(english_db), "search",
                "--city", "Aden", "--type", "restaurants",
                "--providers", "sample", "--no-website-check", "--limit", "20",
            ]
        )
        capsys.readouterr()

        arabic_repo = SQLiteLeadRepository(arabic_db)
        english_repo = SQLiteLeadRepository(english_db)
        arabic_names = {lead.business_name for lead in arabic_repo.find(LeadFilter(city="Aden"))}
        english_names = {lead.business_name for lead in english_repo.find(LeadFilter(city="Aden"))}
        assert arabic_names == english_names
        arabic_repo.close()
        english_repo.close()

    def test_mismatched_country_yields_no_rows(self, tmp_path: Path, capsys):
        db_path = tmp_path / "mismatch.db"
        code = main(
            [
                "--db", str(db_path), "search",
                "--city", "عدن", "--country", "Egypt",
                "--type", "المطاعم", "--providers", "sample",
                "--no-website-check", "--limit", "20",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "No leads found." in out

        repo = SQLiteLeadRepository(db_path)
        assert repo.count() == 0
        repo.close()

    def test_arabic_workflow_exports_json_and_csv(self, tmp_path: Path, capsys):
        """Export the localized results in both formats, as the docs describe."""
        db_path = tmp_path / "export.db"
        self._search(db_path)
        capsys.readouterr()

        json_path = tmp_path / "leads.json"
        csv_path = tmp_path / "leads.csv"
        assert main(["--db", str(db_path), "export", "--format", "json", "--output", str(json_path)]) == 0
        assert main(["--db", str(db_path), "export", "--format", "csv", "--output", str(csv_path)]) == 0
        capsys.readouterr()

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["metadata"]["count"] == 1
        assert payload["leads"][0]["business_name"] == "Al Bahr Seafood Restaurant"

        with csv_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        assert rows[0]["city"] == "Aden"
        assert rows[0]["business_name"] == "Al Bahr Seafood Restaurant"

    def test_list_and_show_work_after_arabic_search(self, tmp_path: Path, capsys):
        db_path = tmp_path / "list.db"
        self._search(db_path)
        capsys.readouterr()

        assert main(["--db", str(db_path), "list", "--city", "عدن"]) == 0
        listed = capsys.readouterr().out
        assert "Al Bahr Seafood Restaurant" in listed

        repo = SQLiteLeadRepository(db_path)
        lead_id = repo.find(LeadFilter(city="Aden"))[0].id
        repo.close()

        assert main(["--db", str(db_path), "show", lead_id]) == 0
        shown = capsys.readouterr().out
        assert "Al Bahr Seafood Restaurant" in shown
        assert "Aden" in shown


class TestGooglePlacesEndToEnd:
    """The real discovery provider, wired through the whole pipeline.

    Only the network transport is faked; the provider, normalizer, scoring,
    storage and CLI are the real implementations.
    """

    def _provider(self, transport, **config):
        from lead_finder_agent.search.providers.google_places import GooglePlacesProvider

        config.setdefault("api_key", "fake-key")
        config["client"] = transport.client()
        return GooglePlacesProvider(config)

    def test_places_to_storage_flow(self, fake_transport, tmp_path: Path):
        from tests.conftest import google_page, google_place

        fake_transport.add(
            "places:searchText",
            body=json.dumps(
                google_page(
                    [
                        google_place(
                            "gp-1", "Aden Fish House", nationalPhoneNumber="+967 1 111 1111"
                        ),
                        google_place(
                            "gp-2", "Aden Bakery", nationalPhoneNumber="+967 2 222 2222"
                        ),
                    ]
                )
            ),
        )

        repo = SQLiteLeadRepository(tmp_path / "gp.db")
        result = LeadFinderPipeline(
            providers=[self._provider(fake_transport)],
            repository=repo,
            check_websites=False,
        ).run(
            SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=20)
        )

        assert result.count == 2
        assert repo.count() == 2
        stored = {lead.business_name: lead for lead in repo.find(LeadFilter(city="Aden"))}
        assert set(stored) == {"Aden Fish House", "Aden Bakery"}

        lead = stored["Aden Fish House"]
        assert lead.source == "google_places"
        assert lead.country == "Yemen"
        assert lead.latitude == pytest.approx(12.7855)
        assert lead.phone
        assert lead.source_url.startswith("https://maps.google.com/")
        repo.close()

    def test_cli_search_with_provider(self, fake_transport, tmp_path: Path, monkeypatch):
        """The documented CLI example, backed by mocked provider data."""
        from tests.conftest import google_page, google_place

        fake_transport.add(
            "places:searchText",
            body=json.dumps(google_page([google_place("gp-1", "Aden Fish House")])),
        )
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
        monkeypatch.setenv("LEAD_FINDER_PROVIDERS", "google_places")

        db_path = tmp_path / "cli-gp.db"
        # Patch the HTTP client the provider builds, so nothing touches the
        # network while the real CLI path is exercised.
        from lead_finder_agent.search.providers import google_places as module

        original_init = module.GooglePlacesProvider.__init__

        def patched_init(self, config=None):
            config = dict(config or {})
            config["client"] = fake_transport.client()
            original_init(self, config)

        monkeypatch.setattr(module.GooglePlacesProvider, "__init__", patched_init)

        code = main(
            [
                "--db", str(db_path), "search",
                "--country", "Yemen", "--city", "Aden",
                "--type", "restaurants", "--limit", "20",
            ]
        )
        assert code == 0

        repo = SQLiteLeadRepository(db_path)
        names = {lead.business_name for lead in repo.find(LeadFilter(city="Aden"))}
        assert "Aden Fish House" in names
        repo.close()

    def test_provider_is_not_location_specific(self, fake_transport, tmp_path: Path):
        """A second, unrelated country/city proves nothing is hard-coded.

        The provider receives the location from the caller, so Lisbon behaves
        exactly like Aden with no code change.
        """
        from tests.conftest import google_page, google_place

        fake_transport.add(
            "places:searchText",
            body=json.dumps(
                google_page(
                    [google_place("pt-1", "Lisboa Bakery", city="Lisbon", country="Portugal")]
                )
            ),
        )

        repo = SQLiteLeadRepository(tmp_path / "pt.db")
        result = LeadFinderPipeline(
            providers=[self._provider(fake_transport)],
            repository=repo,
            check_websites=False,
        ).run(
            SearchQuery(city="Lisbon", country="Portugal", business_type="bakeries", limit=20)
        )

        assert result.count == 1
        stored = repo.find(LeadFilter(city="Lisbon"))
        assert len(stored) == 1
        assert stored[0].country == "Portugal"
        assert stored[0].city == "Lisbon"
        # The request really carried the caller's location, not a default.
        sent = json.loads(fake_transport.requests[-1]["data"])
        assert sent["textQuery"] == "bakeries in Lisbon, Portugal"
        repo.close()

    def test_limit_is_respected_through_the_pipeline(self, fake_transport, tmp_path: Path):
        from tests.conftest import google_page, google_place

        # Names and phones are deliberately distinct: the pipeline's fuzzy
        # de-duplication would otherwise merge near-identical sample records,
        # which is correct behaviour but not what this test is measuring.
        names = ["Alpha Traders", "Bravo Motors", "Charlie Textiles", "Delta Foods"]
        fake_transport.add(
            "places:searchText",
            body=json.dumps(
                google_page(
                    [
                        google_place(f"gp-{i}", name, nationalPhoneNumber=f"+967 3 000 {i}{i}{i}{i}")
                        for i, name in enumerate(names)
                    ]
                )
            ),
        )

        repo = SQLiteLeadRepository(tmp_path / "limit.db")
        result = LeadFinderPipeline(
            providers=[self._provider(fake_transport)],
            repository=repo,
            check_websites=False,
        ).run(SearchQuery(city="Aden", country="Yemen", business_type="shops", limit=3))

        assert result.count == 3
        assert repo.count() == 3
        repo.close()

    def test_missing_key_does_not_break_the_run(self, fake_transport, tmp_path: Path, monkeypatch):
        """A provider without a credential is skipped; others still deliver."""
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)

        from lead_finder_agent.search.providers.google_places import GooglePlacesProvider

        repo = SQLiteLeadRepository(tmp_path / "skip.db")
        result = LeadFinderPipeline(
            providers=[GooglePlacesProvider({}), SampleProvider()],
            repository=repo,
            check_websites=False,
        ).run(SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=20))

        assert result.count >= 1  # the sample provider still answered
        google_response = result.search_result.responses[0]
        assert google_response.error is None
        assert google_response.skipped_reason is not None
        assert "GOOGLE_PLACES_API_KEY" in google_response.skipped_reason
        repo.close()

    def test_rate_limit_is_isolated_and_reported(self, fake_transport, tmp_path: Path):
        fake_transport.add("places:searchText", status=429, body="{}")

        repo = SQLiteLeadRepository(tmp_path / "rate.db")
        result = LeadFinderPipeline(
            providers=[self._provider(fake_transport), SampleProvider()],
            repository=repo,
            check_websites=False,
        ).run(SearchQuery(city="Aden", country="Yemen", business_type="restaurants", limit=20))

        google_response = result.search_result.responses[0]
        assert google_response.error_kind == "rate_limited"
        # The failure is contained: the run still succeeds via the other provider.
        assert result.count >= 1
        repo.close()
