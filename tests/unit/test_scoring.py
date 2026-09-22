"""Tests for the scoring engine and its rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_finder_agent.models import (
    BusinessStatus,
    Confidence,
    LeadPriority,
    WebsiteCheckResult,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.scoring import LeadScorer, build_signals, load_scoring_rules
from lead_finder_agent.scoring.rules import ScoringRule, ScoringRules


class TestRuleMatching:
    def test_rule_fires_when_all_conditions_hold(self):
        rule = ScoringRule(id="r", points=5, when={"a": True, "b": True})
        assert rule.matches({"a": True, "b": True}) is True
        assert rule.matches({"a": True, "b": False}) is False
        assert rule.matches({"a": True}) is False

    def test_rule_supports_numeric_thresholds(self):
        rule = ScoringRule(id="r", points=5, when={"rating": 4.0})
        assert rule.matches({"rating": 4.5}) is True
        assert rule.matches({"rating": 4.0}) is True
        assert rule.matches({"rating": 3.9}) is False
        assert rule.matches({"rating": None}) is False

    def test_from_dict_requires_id_and_points(self):
        with pytest.raises(ValueError):
            ScoringRule.from_dict({"points": 1})
        with pytest.raises(ValueError):
            ScoringRule.from_dict({"id": "x"})


class TestScoringRules:
    def test_defaults_load(self):
        rules = load_scoring_rules()
        assert rules.rules
        assert rules.score_max == 100
        assert rules.thresholds["hot_score"] == 70

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValueError):
            ScoringRules.from_dict(
                {"rules": [{"id": "a", "points": 1}, {"id": "a", "points": 2}]}
            )

    def test_rule_lookup(self):
        rules = load_scoring_rules()
        assert rules.rule("no_website") is not None
        assert rules.rule("does-not-exist") is None

    def test_custom_file_overrides_a_rule(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"rules": [{"id": "no_website", "points": 99, "when": {"website_missing": True}}]}))
        rules = load_scoring_rules(path)
        assert rules.rule("no_website").points == 99
        # Other default rules survive the merge.
        assert rules.rule("business_active") is not None

    def test_custom_file_can_replace_everything(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps({"replace": True, "rules": [{"id": "only", "points": 3, "when": {"has_phone": True}}]})
        )
        rules = load_scoring_rules(path)
        assert len(rules.rules) == 1

    def test_invalid_path_falls_back_to_defaults(self, tmp_path: Path):
        rules = load_scoring_rules(tmp_path / "missing.json")
        assert rules.rule("no_website") is not None


class TestSignals:
    def test_signals_for_business_without_website(self, lead_without_website):
        signals = build_signals(lead_without_website)
        assert signals["website_missing"] is True
        assert signals["has_phone"] is True
        assert signals["business_active"] is True

    def test_signals_read_from_check_result(self, lead_without_website):
        check = WebsiteCheckResult(
            status=WebsiteStatus.EXISTS,
            quality=WebsiteQuality.SOCIAL_ONLY,
            social_only=True,
            is_reachable=True,
        )
        signals = build_signals(lead_without_website, check=check)
        assert signals["has_website"] is True
        assert signals["website_social_only"] is True

    def test_unknown_status_is_conservative(self, make_lead):
        lead = make_lead(website_status=WebsiteStatus.UNKNOWN)
        signals = build_signals(lead)
        assert signals["website_unknown"] is True
        assert signals["website_missing"] is False
        assert signals["website_is_good"] is False


class TestLeadScorer:
    def test_lead_without_website_outranks_lead_with_good_website(
        self, lead_without_website, lead_with_good_website
    ):
        scorer = LeadScorer()
        without = scorer.score(lead_without_website)
        with_site = scorer.score(lead_with_good_website)
        assert without.score > with_site.score

    def test_good_website_rule_applies_negative_points(self, lead_with_good_website):
        result = LeadScorer().score(lead_with_good_website)
        assert result.breakdown.get("good_website", 0) < 0

    def test_closed_business_is_disqualified(self, make_lead):
        lead = make_lead(
            business_name="Closed Shop",
            business_status=BusinessStatus.CLOSED,
            website_status=WebsiteStatus.NOT_FOUND,
        )
        result = LeadScorer().score(lead)
        assert result.score == 0
        assert result.priority == LeadPriority.DISQUALIFIED

    def test_reasons_are_human_readable(self, lead_without_website):
        result = LeadScorer().score(lead_without_website)
        assert any("No website" in reason for reason in result.reasons)

    def test_score_is_clamped_to_configured_range(self, make_lead):
        lead = make_lead(
            business_name="Perfect Lead",
            phone="+967 1",
            email="a@b.com",
            address="Street",
            description="Great business",
            categories=["shop"],
            social_links={"facebook": "https://facebook.com/x"},
            review_count=500,
            rating=5.0,
            business_status=BusinessStatus.ACTIVE,
            website_status=WebsiteStatus.NOT_FOUND,
        )
        result = LeadScorer().score(lead)
        assert 0 <= result.score <= 100

    def test_confidence_low_for_thin_record(self, make_lead):
        lead = make_lead(business_name="Bare Minimum")
        result = LeadScorer().score(lead)
        assert result.confidence == Confidence.LOW

    def test_confidence_high_for_rich_record(self, make_lead):
        lead = make_lead(
            business_name="Rich Record",
            phone="+967 1",
            email="a@b.com",
            address="Street 1",
            description="Description",
            categories=["shop"],
            social_links={"facebook": "https://facebook.com/x"},
            business_status=BusinessStatus.ACTIVE,
            website_status=WebsiteStatus.NOT_FOUND,
        )
        assert LeadScorer().score(lead).confidence == Confidence.HIGH

    def test_low_confidence_never_yields_hot_priority(self, make_lead):
        rules = load_scoring_rules()
        # Force "hot" to require high confidence.
        rules.thresholds["min_confidence_for_priority"] = "high"
        lead = make_lead(
            business_name="Thin Hot",
            phone="+967 1",
            business_status=BusinessStatus.ACTIVE,
            website_status=WebsiteStatus.NOT_FOUND,
        )
        result = LeadScorer(rules=rules).score(lead)
        assert result.priority != LeadPriority.HOT

    def test_score_lead_returns_same_object_with_score_applied(self, lead_without_website):
        scorer = LeadScorer()
        returned = scorer.score_lead(lead_without_website)
        assert returned is lead_without_website
        assert returned.lead_score > 0
        assert returned.score_reason

    def test_score_all_scores_every_lead(self, make_lead):
        leads = [make_lead(business_name=f"Biz {i}") for i in range(3)]
        scored = LeadScorer().score_all(leads)
        assert len(scored) == 3
        assert all(lead.score_reason for lead in scored)

    def test_deterministic_for_same_input(self, lead_without_website):
        scorer = LeadScorer()
        assert scorer.score(lead_without_website).score == scorer.score(lead_without_website).score
