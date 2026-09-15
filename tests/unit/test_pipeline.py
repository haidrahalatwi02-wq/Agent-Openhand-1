"""Tests for the end-to-end pipeline and the agent core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.core import (
    AgentContext,
    LeadFinderAgent,
    LeadFinderPipeline,
    PipelineResult,
    PipelineStats,
)
from lead_finder_agent.models import (
    Lead,
    ProviderKind,
    SearchQuery,
    WebsiteStatus,
)
from lead_finder_agent.scoring import LeadScorer
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.storage import SQLiteLeadRepository
from tests.conftest import FakeTransport


class StaticProvider(BaseSearchProvider):
    """Provider that returns a fixed list of raw records."""

    name = "static"
    kind = ProviderKind.CUSTOM

    def __init__(self, records, as_dict: bool = True):
        super().__init__()
        self.records = records
        self.as_dict = as_dict

    def search_raw(self, query):
        return list(self.records)


class FailingProvider(BaseSearchProvider):
    name = "failing"
    kind = ProviderKind.CUSTOM

    def search_raw(self, query):
        raise RuntimeError("provider exploded")


@pytest.fixture
def repo():
    repository = SQLiteLeadRepository(":memory:")
    yield repository
    repository.close()


@pytest.fixture
def raw_records():
    return [
        {
            "source_id": "node/1",
            "business_name": "Al Bahr Seafood Restaurant",
            "business_type": "restaurant",
            "city": "Aden",
            "country": "Yemen",
            "phone": "+967 71 234 5678",
            "address": "Corniche Road",
            "business_status": "active",
            "rating": 4.4,
            "review_count": 120,
            "social_links": {"facebook": "https://facebook.com/albahr"},
        },
        {
            "source_id": "node/2",
            "business_name": "Modern Electronics Aden",
            "business_type": "electronics",
            "city": "Aden",
            "country": "Yemen",
            "phone": "+967 71 900 4433",
            "website_url": "https://modern.example",
            "business_status": "active",
        },
        {
            # Duplicate of record 1 with a richer description.
            "source_id": "node/1",
            "business_name": "Al Bahr Seafood Restaurant",
            "business_type": "restaurant",
            "city": "Aden",
            "country": "Yemen",
            "phone": "+967 71 234 5678",
            "description": "Family seafood restaurant on the corniche.",
        },
    ]


def _offline_checker(fake_transport: FakeTransport) -> HttpWebsiteChecker:
    return HttpWebsiteChecker(
        client=fake_transport.client(), probe_by_name=False
    )


class TestPipelineStages:
    def test_full_pipeline_produces_scored_stored_leads(
        self, repo, raw_records, fake_transport, good_page_html
    ):
        fake_transport.add("modern.example", status=200, body=good_page_html)
        pipeline = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            checker=_offline_checker(fake_transport),
            scorer=LeadScorer(),
            repository=repo,
        )
        result = pipeline.run(SearchQuery(city="Aden", business_type="restaurant", limit=10))

        assert isinstance(result, PipelineResult)
        assert result.count == 2  # duplicate collapsed
        assert repo.count() == 2
        assert result.stats.raw_count == 3
        assert result.stats.duplicates_removed == 1
        assert result.stats.scored_count == 2
        assert result.stats.stored_count == 2

    def test_leads_without_website_outscore_leads_with_good_website(
        self, repo, raw_records, fake_transport, good_page_html
    ):
        fake_transport.add("modern.example", status=200, body=good_page_html)
        pipeline = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            checker=_offline_checker(fake_transport),
            repository=repo,
        )
        result = pipeline.run(SearchQuery(city="Aden", limit=10))
        by_name = {lead.business_name: lead for lead in result.leads}
        assert by_name["Al Bahr Seafood Restaurant"].lead_score > by_name[
            "Modern Electronics Aden"
        ].lead_score

    def test_website_status_recorded_on_lead(self, repo, raw_records, fake_transport, good_page_html):
        fake_transport.add("modern.example", status=200, body=good_page_html)
        fake_transport.add(".com", status=404, body="")
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            checker=_offline_checker(fake_transport),
            repository=repo,
        ).run(SearchQuery(city="Aden", limit=10))

        by_name = {lead.business_name: lead for lead in result.leads}
        assert by_name["Modern Electronics Aden"].website_status == WebsiteStatus.EXISTS
        assert by_name["Al Bahr Seafood Restaurant"].website_status == WebsiteStatus.UNKNOWN

    def test_scored_leads_are_sorted_by_score(self, repo, raw_records, fake_transport, good_page_html):
        fake_transport.add("modern.example", status=200, body=good_page_html)
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            checker=_offline_checker(fake_transport),
            repository=repo,
        ).run(SearchQuery(city="Aden", limit=10))
        scores = [lead.lead_score for lead in result.leads]
        assert scores == sorted(scores, reverse=True)

    def test_provider_failure_is_isolated(self, repo):
        pipeline = LeadFinderPipeline(
            providers=[FailingProvider(), StaticProvider([{"business_name": "Backup Biz"}])],
            checker=_offline_checker(FakeTransport()),
            repository=repo,
        )
        result = pipeline.run(SearchQuery(city="Aden", limit=10))
        assert result.count == 1
        assert "failing" in result.errors
        assert "search:provider_errors" in result.stats.failed_stages

    def test_all_providers_failing_yields_empty_result_not_exception(self, repo):
        pipeline = LeadFinderPipeline(
            providers=[FailingProvider()],
            checker=_offline_checker(FakeTransport()),
            repository=repo,
        )
        result = pipeline.run(SearchQuery(city="Aden", limit=10))
        assert result.count == 0
        assert result.errors

    def test_no_providers_is_reported(self, repo):
        result = LeadFinderPipeline(
            providers=[], checker=_offline_checker(FakeTransport()), repository=repo
        ).run(SearchQuery(city="Aden", limit=5))
        assert result.count == 0
        assert "search:no_providers" in result.stats.failed_stages

    def test_website_check_can_be_skipped(self, repo, raw_records):
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            repository=repo,
            check_websites=False,
        ).run(SearchQuery(city="Aden", limit=10))
        assert "website_check:skipped" in result.stats.failed_stages
        assert all(lead.website_status == WebsiteStatus.NOT_CHECKED for lead in result.leads)

    def test_max_checks_limits_website_checks(self, repo, raw_records, fake_transport):
        pipeline = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            checker=_offline_checker(fake_transport),
            repository=repo,
            max_checks=1,
        )
        result = pipeline.run(SearchQuery(city="Aden", limit=10))
        assert result.stats.checked_count == 1

    def test_without_repository_results_are_not_stored(self, raw_records):
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)],
            repository=None,
        ).run(SearchQuery(city="Aden", limit=10))
        assert result.count >= 1
        assert result.stats.stored_count == 0

    def test_checker_exception_does_not_abort_run(self, repo, raw_records, monkeypatch):
        checker = _offline_checker(FakeTransport())

        def boom(lead):
            raise RuntimeError("checker down")

        monkeypatch.setattr(checker, "check", boom)
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)], checker=checker, repository=repo
        ).run(SearchQuery(city="Aden", limit=10))
        assert result.count == 2
        assert "website_check:error" in result.stats.failed_stages

    def test_stats_include_provider_breakdown(self, repo, raw_records):
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)], repository=repo, check_websites=False
        ).run(SearchQuery(city="Aden", limit=10))
        assert result.stats.providers
        assert result.stats.providers[0]["provider"] == "static"

    def test_result_round_trips_to_dict(self, repo, raw_records):
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)], repository=repo, check_websites=False
        ).run(SearchQuery(city="Aden", limit=10))
        payload = json.loads(json.dumps(result.to_dict(), default=str))
        assert payload["query"]["city"] == "Aden"
        assert len(payload["leads"]) == 2

    def test_top_returns_highest_scoring(self, repo, raw_records):
        result = LeadFinderPipeline(
            providers=[StaticProvider(raw_records)], repository=repo, check_websites=False
        ).run(SearchQuery(city="Aden", limit=10))
        top = result.top(1)
        assert len(top) == 1
        assert top[0].lead_score >= result.leads[-1].lead_score


class TestPipelineStats:
    def test_to_dict_is_json_safe(self):
        stats = PipelineStats(query=SearchQuery(city="Aden", limit=5))
        assert json.loads(json.dumps(stats.to_dict()))["query"]["city"] == "Aden"


class TestAgentCore:
    def test_agent_runs_from_high_level_input(self, repo, raw_records):
        context = AgentContext(
            providers=[StaticProvider(raw_records)],
            repository=repo,
            checker=_offline_checker(FakeTransport()),
        )
        agent = LeadFinderAgent(context=context, check_websites=False)
        result = agent.run(city="Aden", business_type="restaurant", limit=10)
        assert result.count == 2
        assert agent.name == "lead_finder"

    def test_agent_uses_defaults_for_missing_location(self, repo, settings):
        context = AgentContext(
            settings=settings,
            providers=[StaticProvider([{"business_name": "Default City Biz"}])],
            repository=repo,
        )
        result = LeadFinderAgent(context=context, check_websites=False).run(limit=5)
        assert result.query.city == "Aden"
        assert result.query.country == "Yemen"

    def test_agent_list_and_top_leads(self, repo):
        context = AgentContext(repository=repo, providers=[])
        agent = LeadFinderAgent(context=context, check_websites=False)
        repo.add(Lead(business_name="A", city="Aden", lead_score=10))
        repo.add(Lead(business_name="B", city="Aden", lead_score=90))
        assert len(agent.list_leads(city="Aden")) == 2
        assert agent.top_leads(1)[0].business_name == "B"

    def test_agent_export(self, repo, tmp_path: Path):
        agent = LeadFinderAgent(context=AgentContext(repository=repo, providers=[]))
        repo.add(Lead(business_name="A", city="Aden", lead_score=80))
        target = tmp_path / "out.json"
        written = agent.export(str(target), fmt="json")
        assert Path(written).exists()
        payload = json.loads(target.read_text())
        assert payload["metadata"]["agent"] == "lead_finder"
        assert payload["leads"][0]["business_name"] == "A"

    def test_agent_stats(self, repo):
        agent = LeadFinderAgent(context=AgentContext(repository=repo, providers=[]))
        repo.add(Lead(business_name="A"))
        assert agent.stats()["total"] == 1

    def test_agent_context_creates_repository_lazily(self, settings, tmp_path):
        settings = settings.with_overrides(db_path=tmp_path / "lazy.db")
        context = AgentContext(settings=settings)
        repository = context.resolve_repository()
        assert repository is context.repository
        assert settings.db_path.exists() or settings.db_path.parent.exists()

    def test_agent_context_builds_providers_from_settings(self, settings):
        settings = settings.with_overrides(providers=["sample"])
        context = AgentContext(settings=settings)
        providers = context.resolve_providers()
        assert [p.name for p in providers] == ["sample"]

    def test_default_checker_is_http_checker(self, settings):
        context = AgentContext(settings=settings)
        assert isinstance(context.resolve_checker(), HttpWebsiteChecker)

    def test_default_scorer_uses_scoring_rules(self, settings):
        context = AgentContext(settings=settings)
        assert context.resolve_scorer().rules.rules

    def test_agent_info(self, repo):
        agent = LeadFinderAgent(context=AgentContext(repository=repo, providers=[]))
        assert agent.info()["name"] == "lead_finder"
