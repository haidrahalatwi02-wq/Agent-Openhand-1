"""Integration tests for the Website Analyzer Agent.

The analyzer's unit tests hand-write the stored website-check payload, which
means they would keep passing even if the checker started recording different
field names. These tests close that gap: the payload here is whatever
``HttpWebsiteChecker`` actually writes, so the analyzer and the checker are
verified against each other rather than against a fixture someone typed.

The only stubbed piece is the network transport, so the suite stays offline.
"""

from __future__ import annotations

import pytest

from lead_finder_agent.agents import WebsiteAnalyzerAgent
from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.core import AgentContext, AgentManager
from lead_finder_agent.models import SearchQuery, WebsiteStatus
from lead_finder_agent.search.base import BaseSearchProvider, ProviderKind
from lead_finder_agent.core import LeadFinderPipeline
from lead_finder_agent.storage import SQLiteLeadRepository

from tests.conftest import FakeTransport

pytestmark = pytest.mark.integration


class _Provider(BaseSearchProvider):
    """Returns canned raw records, so a scenario can name its own businesses."""

    name = "analyzer_scenario"
    kind = ProviderKind.CUSTOM

    def __init__(self, routes):
        self.routes = routes

    def search_raw(self, query):
        return self.routes


def run_pipeline(fake_transport: FakeTransport, routes) -> SQLiteLeadRepository:
    """Run the real pipeline over canned records and return the repository."""
    repo = SQLiteLeadRepository(":memory:")
    LeadFinderPipeline(
        providers=[_Provider(routes)],
        checker=HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False),
        repository=repo,
    ).run(SearchQuery(city="Aden", country="Yemen", limit=20))
    return repo


def analyse(repo: SQLiteLeadRepository):
    return WebsiteAnalyzerAgent(AgentContext(repository=repo)).run()


class TestAnalyzerOverRealChecks:
    def test_a_verified_missing_site_is_reported_as_no_website(self, fake_transport):
        fake_transport.add("gone.example", status=404, body="not found")
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Gone Biz", "website_url": "https://gone.example"}],
        )
        assert repo.find()[0].website_status == WebsiteStatus.NOT_FOUND

        analysis = analyse(repo)[0]
        assert "no_website_confirmed" in analysis.finding_kinds()
        assert analysis.needs_attention is True
        assert analysis.from_stored_check is True
        repo.close()

    def test_a_site_we_could_not_reach_is_not_reported_as_missing(self, fake_transport):
        # No route registered for this host, so the transport fails the request.
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Down Biz", "website_url": "https://down.example"}],
        )
        assert repo.find()[0].website_status == WebsiteStatus.UNREACHABLE

        analysis = analyse(repo)[0]
        assert "site_unreachable" in analysis.finding_kinds()
        assert "no_website_confirmed" not in analysis.finding_kinds()
        repo.close()

    def test_an_unchecked_lead_never_becomes_a_confirmed_absence(self, fake_transport):
        """The honesty invariant, end to end through the real components."""
        repo = run_pipeline(fake_transport, [{"business_name": "Unknown Biz"}])
        assert repo.find()[0].website_status == WebsiteStatus.UNKNOWN

        analysis = analyse(repo)[0]
        assert "no_website_confirmed" not in analysis.finding_kinds()
        assert "check_unavailable" in analysis.finding_kinds()
        assert analysis.needs_attention is False
        repo.close()

    def test_a_healthy_site_yields_no_false_findings(self, fake_transport, good_page_html):
        fake_transport.add("traders.example", status=200, body=good_page_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Aden Traders", "website_url": "https://traders.example"}],
        )
        assert repo.find()[0].website_status == WebsiteStatus.EXISTS

        analysis = analyse(repo)[0]
        # The checker recorded these as present, so reporting their absence
        # would be a false claim about a site that was actually fine.
        assert "no_https" not in analysis.finding_kinds()
        assert "no_contact_details" not in analysis.finding_kinds()
        repo.close()

    def test_a_thin_site_is_reported_as_weak(self, fake_transport, thin_page_html):
        fake_transport.add("thin.example", status=200, body=thin_page_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Thin Biz", "website_url": "https://thin.example"}],
        )
        assert repo.find()[0].website_status == WebsiteStatus.EXISTS

        analysis = analyse(repo)[0]
        assert "weak_quality" in analysis.finding_kinds()
        assert analysis.needs_attention is True
        repo.close()

    def test_a_placeholder_domain_is_reported_as_weak(self, fake_transport, placeholder_html):
        fake_transport.add("parked.example", status=200, body=placeholder_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Parked Biz", "website_url": "https://parked.example"}],
        )
        analysis = analyse(repo)[0]
        assert "weak_quality" in analysis.finding_kinds()
        repo.close()

    def test_an_outdated_site_is_reported_as_weak(self, fake_transport, outdated_html):
        fake_transport.add("old.example", status=200, body=outdated_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Old Biz", "website_url": "https://old.example"}],
        )
        analysis = analyse(repo)[0]
        assert "weak_quality" in analysis.finding_kinds()
        repo.close()

    def test_a_social_only_presence_is_flagged_and_not_misreported(self, fake_transport):
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Social Biz", "website_url": "https://facebook.com/socialbiz"}],
        )
        analysis = analyse(repo)[0]
        assert "social_only_presence" in analysis.finding_kinds()
        # A profile was never meant to have a shop, so that absence is noise.
        assert "no_shop" not in analysis.finding_kinds()
        assert "no_contact_details" not in analysis.finding_kinds()
        repo.close()

    def test_findings_stay_consistent_with_the_recorded_check(self, fake_transport, good_page_html):
        """Every finding the analyzer emits must be backed by the stored check."""
        fake_transport.add("traders.example", status=200, body=good_page_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Aden Traders", "website_url": "https://traders.example"}],
        )
        lead = repo.find()[0]
        stored = lead.raw["website_check"]

        analysis = analyse(repo)[0]
        for finding in analysis.findings:
            # The detail claims below are only sound if the payload recorded the
            # field they depend on. Anything else would be invented.
            if finding.kind == "no_https":
                assert stored.get("has_https") is False
            if finding.kind == "no_contact_details":
                assert stored.get("has_contact_page") is False
            if finding.kind == "no_shop":
                assert stored.get("has_shop") is False
        repo.close()


class TestAnalyzerThroughTheManager:
    def test_it_runs_over_leads_the_lead_finder_stored(self, fake_transport, good_page_html):
        fake_transport.add("traders.example", status=200, body=good_page_html)
        repo = run_pipeline(
            fake_transport,
            [
                {"business_name": "Aden Traders", "website_url": "https://traders.example"},
                {"business_name": "Unknown Biz"},
            ],
        )
        context = AgentContext(repository=repo)
        manager = AgentManager(context)
        manager.register(WebsiteAnalyzerAgent(context))

        outcome = manager.run_isolated("website_analyzer")

        assert outcome.ok is True
        assert len(outcome.result) == repo.count()
        repo.close()

    def test_a_confirmed_absence_survives_the_manager_round_trip(self, fake_transport):
        fake_transport.add("gone.example", status=404, body="not found")
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Gone Biz", "website_url": "https://gone.example"}],
        )
        context = AgentContext(repository=repo)
        manager = AgentManager(context)
        manager.register(WebsiteAnalyzerAgent(context))

        outcome = manager.run_isolated("website_analyzer")
        assert "no_website_confirmed" in outcome.result[0].finding_kinds()
        repo.close()

    def test_storing_findings_leaves_the_score_untouched(self, fake_transport, good_page_html):
        fake_transport.add("traders.example", status=200, body=good_page_html)
        repo = run_pipeline(
            fake_transport,
            [{"business_name": "Aden Traders", "website_url": "https://traders.example"}],
        )
        before = repo.find()[0].lead_score

        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo), store=True)
        agent.run()

        stored = repo.find()[0]
        assert stored.lead_score == before
        assert stored.raw["website_analysis"]["findings"] is not None
        repo.close()

    def test_the_cli_analyze_command_works_on_real_checks(self, fake_transport, tmp_path, capsys):
        from lead_finder_agent.cli import main

        db = tmp_path / "analyze.db"
        assert main(["--db", str(db), "search", "--city", "Aden",
                     "--providers", "sample", "--no-website-check", "--limit", "5"]) == 0
        capsys.readouterr()

        assert main(["--db", str(db), "analyze"]) == 0
        out = capsys.readouterr().out
        assert "Findings" in out
        assert "does NOT mean the business has no website" in out
