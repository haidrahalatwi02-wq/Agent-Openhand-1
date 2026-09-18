"""Tests for the Website Analyzer Agent.

Two properties matter more than the rest, and most of these tests exist to pin
them down:

* **Honesty.** An inconclusive check must be reported as a gap in what we know,
  never as proof the business has no website. This is the same invariant the
  scorer protects, and it is easy to break by writing a finding too confidently.
* **Absent data is not a negative.** A stored check that omits a detail (an
  older record, a hand-written payload) must not produce a confident claim about
  that detail. The dataclass defaults are indistinguishable from a recorded
  ``False`` unless the payload itself is consulted.

Everything here runs offline: the one test that reaches the checker injects a
stub, so no request is made.
"""

from __future__ import annotations

import json

import pytest

from lead_finder_agent.agents import (
    SEVERITY_ORDER,
    WebsiteAnalysis,
    WebsiteAnalyzerAgent,
    WebsiteFinding,
    load_analysis_config,
)
from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.core import AgentContext, AgentManager
from lead_finder_agent.models import (
    Lead,
    WebsiteCheckResult,
    WebsiteErrorKind,
    WebsiteIdentity,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.search.providers.sample import SampleProvider
from lead_finder_agent.storage.sqlite_repository import SQLiteLeadRepository


def stored_lead(
    name: str = "Test Business",
    status: WebsiteStatus = WebsiteStatus.EXISTS,
    quality: WebsiteQuality = WebsiteQuality.GOOD,
    payload: dict | None = None,
    **lead_kwargs,
) -> Lead:
    """A lead carrying a stored website check in its raw payload."""
    lead = Lead(business_name=name, city="Aden", country="Yemen", **lead_kwargs)
    lead.website_status = status
    lead.website_quality = quality
    check = {"status": str(status), "quality": str(quality)}
    check.update(payload or {})
    lead.raw["website_check"] = check
    return lead


def a_good_site_payload() -> dict:
    """Every detail a healthy site records, so no finding fires by omission."""
    return {
        "has_https": True,
        "has_contact_page": True,
        "has_shop": True,
        "page_title": "Aden Traders - Home",
        "response_time_ms": 120,
        "truncated": False,
    }


class StubChecker(BaseWebsiteChecker):
    """Counts calls so a test can prove the network was not touched."""

    name = "stub"

    def __init__(self, result: WebsiteCheckResult | None = None) -> None:
        self.calls = 0
        self._result = result

    def check(self, lead: Lead) -> WebsiteCheckResult:
        self.calls += 1
        return self._result or WebsiteCheckResult(
            status=WebsiteStatus.NOT_FOUND,
            quality=WebsiteQuality.UNKNOWN,
            website_url=lead.website_url,
        )


@pytest.fixture
def agent() -> WebsiteAnalyzerAgent:
    return WebsiteAnalyzerAgent(AgentContext())


class TestAgentContract:
    def test_the_agent_implements_the_base_contract(self):
        instance = WebsiteAnalyzerAgent()
        assert instance.name == "website_analyzer"
        assert instance.description
        assert instance.info() == {
            "name": "website_analyzer",
            "description": "Analyse the quality and identity of a lead's website",
        }

    def test_the_agent_is_read_only_by_default(self):
        assert WebsiteAnalyzerAgent().store is False

    def test_it_can_be_registered_with_the_manager(self):
        manager = AgentManager()
        manager.register(WebsiteAnalyzerAgent(manager.context))
        assert "website_analyzer" in manager
        assert isinstance(manager.get("website_analyzer"), WebsiteAnalyzerAgent)

    def test_it_is_part_of_the_default_agent_set(self):
        manager = AgentManager().register_default_agents()
        assert manager.names() == ["lead_finder", "website_analyzer"]

    def test_it_shares_the_managers_context(self):
        context = AgentContext()
        manager = AgentManager(context).register_default_agents()
        assert manager.get("website_analyzer").context is context

    def test_registering_defaults_twice_stays_idempotent(self):
        manager = AgentManager().register_default_agents()
        manager.register_default_agents()
        assert len(manager) == 2

    def test_it_can_be_replaced_deliberately(self):
        manager = AgentManager().register_default_agents()
        replacement = WebsiteAnalyzerAgent(manager.context)
        manager.register(replacement, replace=True)
        assert manager.get("website_analyzer") is replacement


class TestAnalysisConfig:
    def test_thresholds_and_severities_load_from_packaged_data(self):
        config = load_analysis_config()
        assert config["thresholds"]["slow_response_ms"] > 0
        assert config["severities"]["social_only_presence"] == "high"

    def test_every_severity_is_a_known_level(self):
        for kind, severity in load_analysis_config()["severities"].items():
            assert severity in SEVERITY_ORDER, f"{kind} has unknown severity {severity}"

    def test_an_override_replaces_one_section_without_restating_the_other(self):
        config = load_analysis_config({"thresholds": {"slow_response_ms": 10}})
        assert config["thresholds"]["slow_response_ms"] == 10
        assert config["thresholds"]["min_title_length"] == 3
        assert config["severities"]["no_https"] == "medium"

    def test_the_agent_exposes_its_configured_thresholds(self):
        agent = WebsiteAnalyzerAgent(config={"thresholds": {"slow_response_ms": 42}})
        assert agent._threshold("slow_response_ms", 0) == 42

    def test_a_missing_threshold_falls_back_to_the_caller_default(self):
        assert WebsiteAnalyzerAgent()._threshold("not_a_threshold", "fallback") == "fallback"

    def test_severities_come_from_config(self):
        agent = WebsiteAnalyzerAgent(config={"severities": {"no_https": "high"}})
        assert agent._severity("no_https") == "high"

    def test_an_unknown_finding_kind_defaults_to_low(self):
        assert WebsiteAnalyzerAgent()._severity("never_configured") == "low"


class TestHonestyAboutInconclusiveChecks:
    """The core invariant: not knowing is not the same as knowing there is none."""

    @pytest.mark.parametrize(
        "status", [WebsiteStatus.UNKNOWN, WebsiteStatus.NOT_CHECKED]
    )
    def test_an_inconclusive_check_never_claims_there_is_no_website(self, agent, status):
        analysis = agent.analyse(stored_lead(status=status, quality=WebsiteQuality.UNKNOWN))
        assert "no_website_confirmed" not in analysis.finding_kinds()

    @pytest.mark.parametrize(
        "status", [WebsiteStatus.UNKNOWN, WebsiteStatus.NOT_CHECKED]
    )
    def test_an_inconclusive_check_is_reported_as_our_own_gap(self, agent, status):
        analysis = agent.analyse(stored_lead(status=status, quality=WebsiteQuality.UNKNOWN))
        assert "check_unavailable" in analysis.finding_kinds()
        detail = next(f for f in analysis.findings if f.kind == "check_unavailable")
        assert "not that the business lacks a website" in detail.detail

    def test_an_inconclusive_check_does_not_raise_attention(self, agent):
        # It is not a problem with the business, so it must not be flagged as one.
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.NOT_CHECKED))
        assert analysis.needs_attention is False

    def test_a_confirmed_absence_does_claim_there_is_no_website(self, agent):
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.NOT_FOUND))
        assert "no_website_confirmed" in analysis.finding_kinds()
        assert analysis.needs_attention is True

    def test_a_confirmed_absence_is_severe(self, agent):
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.NOT_FOUND))
        finding = next(f for f in analysis.findings if f.kind == "no_website_confirmed")
        assert finding.severity == "high"

    def test_an_unreachable_site_is_not_reported_as_missing(self, agent):
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.UNREACHABLE))
        assert "site_unreachable" in analysis.finding_kinds()
        assert "no_website_confirmed" not in analysis.finding_kinds()

    def test_an_inconclusive_check_is_never_described_as_a_weak_site(self, agent):
        # Weakness is a property of a site we reached; we reached nothing here.
        analysis = agent.analyse(
            stored_lead(status=WebsiteStatus.UNKNOWN, quality=WebsiteQuality.UNKNOWN)
        )
        assert "weak_quality" not in analysis.finding_kinds()


class TestAbsentDataIsNotANegative:
    """A detail the record never captured must not become a confident finding."""

    def test_a_payload_without_https_does_not_report_https(self, agent):
        lead = stored_lead(payload={"page_title": "Fine Site"})
        analysis = agent.analyse(lead)
        assert "no_https" not in analysis.finding_kinds()

    def test_a_payload_without_contact_detail_does_not_report_its_absence(self, agent):
        lead = stored_lead(payload={"page_title": "Fine Site"})
        assert "no_contact_details" not in agent.analyse(lead).finding_kinds()

    def test_a_payload_without_shop_detail_does_not_report_its_absence(self, agent):
        lead = stored_lead(payload={"page_title": "Fine Site"})
        assert "no_shop" not in agent.analyse(lead).finding_kinds()

    def test_a_payload_without_a_response_time_does_not_report_slowness(self, agent):
        lead = stored_lead(payload={"page_title": "Fine Site"})
        assert "slow_response" not in agent.analyse(lead).finding_kinds()

    def test_a_payload_without_a_title_does_not_report_a_missing_title(self, agent):
        lead = stored_lead(payload={"has_https": True})
        assert "missing_title" not in agent.analyse(lead).finding_kinds()

    def test_a_recorded_negative_is_reported(self, agent):
        lead = stored_lead(payload={"has_https": False, "page_title": "Old Site"})
        assert "no_https" in agent.analyse(lead).finding_kinds()

    def test_a_recorded_false_is_not_confused_with_an_absent_key(self, agent):
        # ``truncated: False`` is a recorded negative; it must stay silent.
        with_key = stored_lead(payload={**a_good_site_payload(), "truncated": False})
        assert "truncated_response" not in agent.analyse(with_key).finding_kinds()

    def test_a_recorded_truncation_is_reported(self, agent):
        lead = stored_lead(payload={**a_good_site_payload(), "truncated": True})
        assert "truncated_response" in agent.analyse(lead).finding_kinds()

    def test_a_null_detail_is_treated_as_absent(self, agent):
        lead = stored_lead(payload={**a_good_site_payload(), "response_time_ms": None})
        assert "slow_response" not in agent.analyse(lead).finding_kinds()

    def test_a_good_site_with_full_detail_produces_no_findings(self, agent):
        lead = stored_lead(payload=a_good_site_payload())
        analysis = agent.analyse(lead)
        assert analysis.findings == []
        assert analysis.needs_attention is False
        assert analysis.from_stored_check is True


class TestFindingsFromTheStoredCheck:
    def test_a_social_only_presence_is_high_severity(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.SOCIAL_ONLY,
            payload={"page_title": "Facebook"},
        )
        analysis = agent.analyse(lead)
        assert "social_only_presence" in analysis.finding_kinds()
        finding = next(f for f in analysis.findings if f.kind == "social_only_presence")
        assert finding.severity == "high"

    def test_a_social_profile_is_not_reported_as_lacking_a_shop(self, agent):
        # A profile was never meant to have a shop or a contact page, so
        # reporting those absences would be noise about the wrong thing.
        lead = stored_lead(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.SOCIAL_ONLY,
            payload={"has_shop": False, "has_contact_page": False},
        )
        kinds = agent.analyse(lead).finding_kinds()
        assert "no_shop" not in kinds
        assert "no_contact_details" not in kinds

    def test_a_weak_site_is_reported(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.WEAK,
            payload=a_good_site_payload(),
        )
        assert "weak_quality" in agent.analyse(lead).finding_kinds()

    def test_weak_quality_is_only_reported_for_a_site_we_reached(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.UNREACHABLE, quality=WebsiteQuality.WEAK
        )
        assert "weak_quality" not in agent.analyse(lead).finding_kinds()

    def test_a_slow_site_is_reported_against_the_configured_threshold(self):
        agent = WebsiteAnalyzerAgent(config={"thresholds": {"slow_response_ms": 500}})
        lead = stored_lead(payload={**a_good_site_payload(), "response_time_ms": 900})
        assert "slow_response" in agent.analyse(lead).finding_kinds()

    def test_a_site_within_the_threshold_is_not_reported_as_slow(self):
        agent = WebsiteAnalyzerAgent(config={"thresholds": {"slow_response_ms": 500}})
        lead = stored_lead(payload={**a_good_site_payload(), "response_time_ms": 100})
        assert "slow_response" not in agent.analyse(lead).finding_kinds()

    def test_a_missing_title_is_reported(self, agent):
        lead = stored_lead(payload={**a_good_site_payload(), "page_title": ""})
        assert "missing_title" in agent.analyse(lead).finding_kinds()

    def test_an_uncertain_identity_is_reported(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.GOOD,
            payload={**a_good_site_payload(), "identity": str(WebsiteIdentity.UNCERTAIN)},
        )
        assert "identity_uncertain" in agent.analyse(lead).finding_kinds()

    def test_a_provided_identity_is_not_reported_as_uncertain(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.GOOD,
            payload={**a_good_site_payload(), "identity": str(WebsiteIdentity.PROVIDED)},
        )
        assert "identity_uncertain" not in agent.analyse(lead).finding_kinds()

    def test_an_invalid_url_is_reported_as_unavailable_not_as_missing(self, agent):
        lead = stored_lead(
            status=WebsiteStatus.UNKNOWN,
            quality=WebsiteQuality.UNKNOWN,
            payload={"error_kind": str(WebsiteErrorKind.INVALID_URL)},
        )
        kinds = agent.analyse(lead).finding_kinds()
        assert "check_unavailable" in kinds
        assert "no_website_confirmed" not in kinds


class TestAnalysisWithoutAStoredCheck:
    """Falling back to the lead's own fields, exactly as the scorer does."""

    def test_it_falls_back_to_the_lead_status(self, agent):
        lead = Lead(business_name="Never Checked")
        lead.website_status = WebsiteStatus.NOT_CHECKED
        analysis = agent.analyse(lead)
        assert analysis.status == WebsiteStatus.NOT_CHECKED
        assert analysis.from_stored_check is False

    def test_the_fallback_still_respects_the_honesty_invariant(self, agent):
        lead = Lead(business_name="Never Checked")
        lead.website_status = WebsiteStatus.NOT_CHECKED
        assert "no_website_confirmed" not in agent.analyse(lead).finding_kinds()

    def test_a_plain_http_url_is_reported_as_lacking_https(self, agent):
        lead = Lead(business_name="Old Site", website_url="http://old.example")
        lead.website_status = WebsiteStatus.EXISTS
        assert "no_https" in agent.analyse(lead).finding_kinds()

    def test_an_https_url_produces_no_https_finding(self, agent):
        lead = Lead(business_name="New Site", website_url="https://new.example")
        lead.website_status = WebsiteStatus.EXISTS
        assert "no_https" not in agent.analyse(lead).finding_kinds()

    def test_a_stored_check_takes_precedence_over_the_leads_own_fields(self, agent):
        # The check is the recorded evidence; the lead's field may be stale.
        lead = stored_lead(status=WebsiteStatus.EXISTS, payload=a_good_site_payload())
        lead.website_status = WebsiteStatus.NOT_CHECKED
        analysis = agent.analyse(lead)
        assert analysis.status == WebsiteStatus.EXISTS
        assert analysis.from_stored_check is True


class TestRechecking:
    def test_no_check_is_made_unless_recheck_is_requested(self, agent):
        checker = StubChecker()
        agent.context.checker = checker
        lead = Lead(business_name="Needs Check", website_url="https://x.example")
        agent.analyse(lead, recheck=False)
        assert checker.calls == 0

    def test_recheck_uses_the_injected_checker(self, agent):
        checker = StubChecker()
        agent.context.checker = checker
        lead = Lead(business_name="Needs Check", website_url="https://x.example")
        analysis = agent.analyse(lead, recheck=True)
        assert checker.calls == 1
        assert analysis.status == WebsiteStatus.NOT_FOUND

    def test_an_existing_stored_check_is_not_rechecked(self, agent):
        checker = StubChecker()
        agent.context.checker = checker
        agent.analyse(stored_lead(payload=a_good_site_payload()), recheck=True)
        assert checker.calls == 0

    def test_a_failing_check_degrades_instead_of_raising(self, agent):
        class ExplodingChecker(BaseWebsiteChecker):
            name = "exploding"

            def check(self, lead):
                raise RuntimeError("network is on fire")

        agent.context.checker = ExplodingChecker()
        lead = Lead(business_name="Boom")
        lead.website_status = WebsiteStatus.NOT_CHECKED
        analysis = agent.analyse(lead, recheck=True)
        assert analysis.status == WebsiteStatus.NOT_CHECKED
        assert "no_website_confirmed" not in analysis.finding_kinds()


class TestRunningOverStoredLeads:
    def _repository_with(self, leads):
        repo = SQLiteLeadRepository(":memory:")
        for lead in leads:
            repo.add(lead)
        return repo

    def test_it_analyses_every_stored_lead(self):
        repo = self._repository_with(
            [
                stored_lead(name="A", payload=a_good_site_payload()),
                stored_lead(name="B", status=WebsiteStatus.NOT_FOUND),
            ]
        )
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        analyses = agent.run()
        assert len(analyses) == 2
        assert {a.business_name for a in analyses} == {"A", "B"}
        repo.close()

    def test_the_limit_bounds_how_many_are_analysed(self):
        repo = self._repository_with(
            [stored_lead(name=f"B{i}", payload=a_good_site_payload()) for i in range(5)]
        )
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        assert len(agent.run(limit=2)) == 2
        repo.close()

    def test_a_negative_limit_is_rejected(self):
        agent = WebsiteAnalyzerAgent(AgentContext())
        with pytest.raises(ValueError):
            agent.run(limit=-1)

    def test_a_status_filter_selects_only_those_leads(self):
        repo = self._repository_with(
            [
                stored_lead(name="Found", status=WebsiteStatus.NOT_FOUND),
                stored_lead(name="Fine", payload=a_good_site_payload()),
            ]
        )
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        analyses = agent.run(statuses=[WebsiteStatus.NOT_FOUND])
        assert [a.business_name for a in analyses] == ["Found"]
        repo.close()

    def test_a_status_filter_applies_the_limit_after_filtering(self):
        # Otherwise a limit smaller than the number of filtered-out leads would
        # return fewer results than the caller asked for.
        repo = self._repository_with(
            [stored_lead(name=f"Fine{i}", payload=a_good_site_payload()) for i in range(3)]
            + [stored_lead(name="Found", status=WebsiteStatus.NOT_FOUND)]
        )
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        analyses = agent.run(limit=1, statuses=[WebsiteStatus.NOT_FOUND])
        assert [a.business_name for a in analyses] == ["Found"]
        repo.close()

    def test_a_single_lead_can_be_analysed_by_id(self):
        lead = stored_lead(name="Target", status=WebsiteStatus.NOT_FOUND)
        repo = self._repository_with([lead, stored_lead(name="Other")])
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        analyses = agent.run(lead_id=lead.id)
        assert [a.business_name for a in analyses] == ["Target"]
        repo.close()

    def test_an_unknown_lead_id_returns_nothing(self):
        repo = self._repository_with([stored_lead(name="A")])
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        assert agent.run(lead_id="does-not-exist") == []
        repo.close()

    def test_min_severity_filters_out_quieter_leads(self):
        repo = self._repository_with(
            [
                stored_lead(name="Serious", status=WebsiteStatus.NOT_FOUND),
                stored_lead(name="Quiet", payload=a_good_site_payload()),
            ]
        )
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        analyses = agent.run(min_severity="high")
        assert [a.business_name for a in analyses] == ["Serious"]
        repo.close()

    def test_an_unknown_min_severity_is_rejected(self):
        agent = WebsiteAnalyzerAgent(AgentContext())
        with pytest.raises(ValueError):
            agent.run(min_severity="catastrophic")

    def test_it_does_not_write_to_the_repository_by_default(self):
        repo = self._repository_with([stored_lead(name="A", status=WebsiteStatus.NOT_FOUND)])
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo))
        agent.run()
        stored = repo.find()[0]
        assert "website_analysis" not in (stored.raw or {})
        repo.close()

    def test_store_persists_the_findings_when_asked(self):
        repo = self._repository_with([stored_lead(name="A", status=WebsiteStatus.NOT_FOUND)])
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo), store=True)
        agent.run()
        stored = repo.find()[0]
        assert stored.raw["website_analysis"]["needs_attention"] is True
        assert "no_website_confirmed" in [
            f["kind"] for f in stored.raw["website_analysis"]["findings"]
        ]
        repo.close()

    def test_storing_does_not_change_the_lead_score(self):
        # Findings are descriptive; a stored score must not depend on whether
        # the analyzer ran.
        lead = stored_lead(name="A", status=WebsiteStatus.NOT_FOUND)
        lead.lead_score = 55
        repo = self._repository_with([lead])
        agent = WebsiteAnalyzerAgent(AgentContext(repository=repo), store=True)
        agent.run()
        assert repo.find()[0].lead_score == 55
        repo.close()


class TestAnalysisResultShape:
    def test_the_result_serializes_to_json(self, agent):
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.NOT_FOUND))
        payload = json.loads(json.dumps(analysis.to_dict()))
        assert payload["status"] == "website_not_found"
        assert payload["needs_attention"] is True
        assert payload["findings"]

    def test_finding_kinds_and_severity_counts(self, agent):
        analysis = agent.analyse(
            stored_lead(
                status=WebsiteStatus.EXISTS,
                quality=WebsiteQuality.WEAK,
                payload={"has_https": False, "page_title": "Old"},
            )
        )
        assert "weak_quality" in analysis.finding_kinds()
        assert analysis.severity_counts().get("medium", 0) >= 1

    def test_the_summary_names_the_findings(self, agent):
        analysis = agent.analyse(stored_lead(status=WebsiteStatus.NOT_FOUND))
        assert "no_website_confirmed" in analysis.summary()

    def test_the_summary_says_so_when_there_is_nothing_to_report(self, agent):
        analysis = agent.analyse(stored_lead(payload=a_good_site_payload()))
        assert "no findings" in analysis.summary()

    def test_a_finding_serializes_with_its_severity(self):
        finding = WebsiteFinding(kind="k", severity="high", detail="d")
        assert finding.to_dict() == {"kind": "k", "severity": "high", "detail": "d"}

    def test_the_analysis_is_a_plain_result_object(self, agent):
        assert isinstance(agent.analyse(stored_lead()), WebsiteAnalysis)


class TestEndToEndThroughTheManager:
    """The analyzer over leads the Lead Finder actually produced."""

    def test_the_analyzer_reads_leads_the_lead_finder_stored(self, settings):
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(
            settings=settings, repository=repo, providers=[SampleProvider()]
        )
        manager = AgentManager(context).register_default_agents()

        found = manager.run("lead_finder", city="Aden", limit=5, check_websites=False)
        assert found.leads

        analyses = manager.run("website_analyzer")
        assert analyses
        # Every analysed lead came from the shared repository, not a private one.
        assert len(analyses) == repo.count()
        repo.close()

    def test_the_two_agents_fan_out_over_one_shared_context(self, settings):
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(
            settings=settings, repository=repo, providers=[SampleProvider()]
        )
        manager = AgentManager(context).register_default_agents()

        results = {
            r.agent: r
            for r in manager.run_all(
                ["lead_finder", "website_analyzer"],
                kwargs_by_agent={"website_analyzer": {"limit": 5}},
                city="Aden",
                limit=5,
                check_websites=False,
            )
        }

        assert results["lead_finder"].ok is True
        assert results["website_analyzer"].ok is True
        assert results["lead_finder"].result.leads
        assert results["website_analyzer"].result
        repo.close()

    def test_an_analysed_lead_never_claims_no_website_without_evidence(self, settings):
        # Leads searched with website checking off are NOT_CHECKED, which is
        # inconclusive; the analyzer must not turn that into a confirmed absence.
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(
            settings=settings, repository=repo, providers=[SampleProvider()]
        )
        manager = AgentManager(context).register_default_agents()
        manager.run("lead_finder", city="Aden", limit=5, check_websites=False)

        for analysis in manager.run("website_analyzer"):
            assert "no_website_confirmed" not in analysis.finding_kinds()
        repo.close()
