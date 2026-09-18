"""Tests for the scoring engine and its rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_finder_agent.models import (
    BusinessStatus,
    Confidence,
    Lead,
    LeadPriority,
    LeadScore,
    WebsiteCheckResult,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.scoring import (
    LeadScorer,
    build_signals,
    hydrate_website_check,
    load_scoring_rules,
)
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

    def test_overlay_keeps_unchanged_defaults_and_order(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"rules": [{"id": "no_website", "points": 50}]}))
        rules = load_scoring_rules(path)
        assert rules.rule("no_website").points == 50
        # Every default rule survives, in its original position.
        assert [r.id for r in rules.rules] == [r.id for r in load_scoring_rules().rules]

    def test_overlay_preserves_the_overridden_rules_category(self, tmp_path: Path):
        # Packaged defaults declare no categories, so the inheritance rule is
        # pinned at the level the merge actually uses: from_dict falls back to
        # the supplied category when the payload omits one. Without that fallback
        # an override which only changes points would reset the category of the
        # rule it replaces back to "general".
        inherited = ScoringRule.from_dict({"id": "grouped", "points": 99}, category="website")
        assert inherited.category == "website"

        explicit = ScoringRule.from_dict(
            {"id": "grouped", "points": 99, "category": "custom"}, category="website"
        )
        assert explicit.category == "custom"

    def test_overlay_keeps_a_new_rules_own_category(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"rules": [{"id": "brand_new", "points": 7, "category": "custom"}]}))
        rules = load_scoring_rules(path)
        assert rules.rule("brand_new").category == "custom"

    def test_overlay_appends_a_genuinely_new_rule(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {"rules": [{"id": "brand_new", "points": 7, "when": {"has_phone": True}}]}
            )
        )
        rules = load_scoring_rules(path)
        assert rules.rule("brand_new").points == 7
        # Appended after the defaults rather than replacing them.
        assert rules.rules[-1].id == "brand_new"
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


# --------------------------------------------------------------------------- #
# Honesty invariants
#
# The scoring layer exists to answer one question honestly: how good a prospect
# is this, given what was actually verified? These tests pin the two properties
# that matter more than any particular weight.
# --------------------------------------------------------------------------- #


class TestUnverifiedIsNotMissing:
    """`we could not verify a website` must never score as `it has none`."""

    def test_confirmed_absence_outranks_unverified(self, make_lead):
        scorer = LeadScorer()
        confirmed = scorer.score(make_lead(business_name="Confirmed", website_status=WebsiteStatus.NOT_FOUND))
        unverified = scorer.score(make_lead(business_name="Unverified", website_status=WebsiteStatus.NOT_CHECKED))
        assert confirmed.score > unverified.score

    def test_only_confirmed_absence_earns_the_missing_website_rule(self, make_lead):
        scorer = LeadScorer()
        for status in (WebsiteStatus.NOT_CHECKED, WebsiteStatus.UNKNOWN, WebsiteStatus.UNREACHABLE):
            result = scorer.score(make_lead(business_name="X", website_status=status))
            assert "no_website" not in result.breakdown

        confirmed = scorer.score(make_lead(business_name="X", website_status=WebsiteStatus.NOT_FOUND))
        assert "no_website" in confirmed.breakdown

    def test_unknown_is_never_reported_as_website_missing(self, make_lead):
        signals = build_signals(make_lead(business_name="X", website_status=WebsiteStatus.UNKNOWN))
        assert signals["website_missing"] is False
        assert signals["website_unknown"] is True

    def test_unreachable_is_not_reported_as_website_missing(self, make_lead):
        signals = build_signals(make_lead(business_name="X", website_status=WebsiteStatus.UNREACHABLE))
        assert signals["website_missing"] is False
        assert signals["website_unreachable"] is True

    def test_a_good_website_is_never_reported_as_missing(self, lead_with_good_website):
        signals = build_signals(lead_with_good_website)
        assert signals["website_missing"] is False
        assert signals["website_is_good"] is True

    def test_an_unreachable_check_never_becomes_not_found(self, make_lead):
        check = WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE, is_reachable=False, error="timeout"
        )
        signals = build_signals(make_lead(business_name="X", website_url="https://x.example"), check=check)
        assert signals["website_missing"] is False
        assert signals["website_unreachable"] is True


class TestPrimeProspectPriority:
    """`hot` is reserved for leads whose data actually established a gap."""

    def _rich(self, make_lead, status, quality=WebsiteQuality.UNKNOWN):
        return make_lead(
            business_name="Rich Record",
            business_type="restaurant",
            address="1 Main St",
            phone="+967 1 234",
            email="a@b.com",
            description="A well documented business",
            categories=["restaurant"],
            source="osm",
            social_links={"facebook": "https://facebook.com/x"},
            rating=4.7,
            review_count=210,
            business_status=BusinessStatus.ACTIVE,
            website_status=status,
            website_quality=quality,
        )

    def test_unverified_rich_record_is_not_hot(self, make_lead):
        scorer = LeadScorer()
        for status in (WebsiteStatus.NOT_CHECKED, WebsiteStatus.UNKNOWN, WebsiteStatus.UNREACHABLE):
            result = scorer.score(self._rich(make_lead, status))
            assert result.priority != LeadPriority.HOT, status

    def test_a_good_working_website_is_never_hot(self, make_lead):
        result = LeadScorer().score(
            self._rich(make_lead, WebsiteStatus.EXISTS, WebsiteQuality.GOOD)
        )
        assert result.priority != LeadPriority.HOT

    def test_confirmed_absence_is_hot(self, make_lead):
        result = LeadScorer().score(self._rich(make_lead, WebsiteStatus.NOT_FOUND))
        assert result.priority == LeadPriority.HOT

    def test_a_weak_website_is_a_prime_prospect(self, make_lead):
        result = LeadScorer().score(
            self._rich(make_lead, WebsiteStatus.EXISTS, WebsiteQuality.WEAK)
        )
        assert result.priority == LeadPriority.HOT

    def test_a_social_only_presence_is_a_prime_prospect(self, make_lead):
        result = LeadScorer().score(
            self._rich(make_lead, WebsiteStatus.EXISTS, WebsiteQuality.SOCIAL_ONLY)
        )
        assert result.priority == LeadPriority.HOT

    def test_closed_business_is_disqualified_however_rich(self, make_lead):
        lead = self._rich(make_lead, WebsiteStatus.NOT_FOUND)
        lead.business_status = BusinessStatus.CLOSED
        result = LeadScorer().score(lead)
        assert result.priority == LeadPriority.DISQUALIFIED


class TestPriorityBuckets:
    def test_score_at_the_hot_threshold_is_hot(self, make_lead):
        rules = load_scoring_rules()
        rules.thresholds["hot_score"] = 50
        result = LeadScorer(rules=rules).score(
            make_lead(
                business_name="X",
                city="A",
                country="B",
                phone="+967 1",
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.score >= 50
        assert result.priority == LeadPriority.HOT

    def test_just_below_the_hot_threshold_is_warm(self, make_lead):
        rules = load_scoring_rules()
        rules.thresholds["hot_score"] = 999  # nothing can be hot
        result = LeadScorer(rules=rules).score(
            make_lead(
                business_name="X",
                city="A",
                country="B",
                phone="+967 1",
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.priority == LeadPriority.WARM

    def test_a_thin_record_falls_to_disqualified(self, make_lead):
        result = LeadScorer().score(make_lead(business_name="Bare Minimum"))
        assert result.score < 20
        assert result.priority == LeadPriority.DISQUALIFIED

    def test_closed_business_scores_disqualified(self, make_lead):
        result = LeadScorer().score(
            make_lead(
                business_name="Closed",
                business_status=BusinessStatus.CLOSED,
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.priority == LeadPriority.DISQUALIFIED

    def test_low_confidence_never_yields_hot_or_warm(self, make_lead):
        rules = load_scoring_rules()
        rules.thresholds["min_confidence_for_priority"] = "high"
        result = LeadScorer(rules=rules).score(
            make_lead(
                business_name="Thin",
                city="A",
                country="B",
                phone="+967 1",
                business_status=BusinessStatus.ACTIVE,
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.priority == LeadPriority.COLD


class TestConfidenceBands:
    def test_thin_record_is_low_confidence(self, make_lead):
        assert LeadScorer().score(make_lead(business_name="Bare")).confidence == Confidence.LOW

    def test_mid_record_is_medium_confidence(self, make_lead):
        result = LeadScorer().score(
            make_lead(
                business_name="Mid",
                city="A",
                country="B",
                phone="+967 1",
                email="a@b.com",
                business_status=BusinessStatus.ACTIVE,
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.confidence == Confidence.MEDIUM

    def test_rich_record_is_high_confidence(self, make_lead):
        result = LeadScorer().score(
            make_lead(
                business_name="Rich",
                city="A",
                country="B",
                phone="+967 1",
                email="a@b.com",
                address="Street",
                description="Description",
                categories=["shop"],
                social_links={"facebook": "https://facebook.com/x"},
                business_status=BusinessStatus.ACTIVE,
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert result.confidence == Confidence.HIGH

    def test_an_inconclusive_check_adds_no_confidence(self, make_lead):
        # Only a conclusive check counts as knowledge, so the same record scores
        # one signal lower when the website status is merely UNKNOWN.
        base = dict(
            business_name="X",
            city="A",
            country="B",
            email="a@b.com",
            address="Street",
            business_status=BusinessStatus.ACTIVE,
        )
        scorer = LeadScorer()
        unverified = scorer.score(make_lead(**base, website_status=WebsiteStatus.UNKNOWN))
        confirmed = scorer.score(make_lead(**base, website_status=WebsiteStatus.NOT_FOUND))
        assert build_signals(make_lead(**base, website_status=WebsiteStatus.UNKNOWN))["website_status_known"] is False
        assert build_signals(make_lead(**base, website_status=WebsiteStatus.NOT_FOUND))["website_status_known"] is True
        # That one signal is exactly what lifts the record into the next band.
        assert unverified.confidence == Confidence.LOW
        assert confirmed.confidence == Confidence.MEDIUM


class TestReasonsAndBreakdown:
    def test_breakdown_lists_exactly_the_fired_rules(self, make_lead):
        rules = load_scoring_rules()
        lead = make_lead(
            business_name="X",
            city="A",
            country="B",
            phone="+967 1",
            website_status=WebsiteStatus.NOT_FOUND,
        )
        result = LeadScorer(rules=rules).score(lead)
        signals = build_signals(lead, rules=rules)
        expected = {r.id for r in rules.rules if r.matches(signals)}
        assert set(result.breakdown) == expected

    def test_breakdown_sums_to_the_unclamped_score(self, make_lead):
        result = LeadScorer().score(
            make_lead(
                business_name="X",
                city="A",
                country="B",
                phone="+967 1",
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert sum(result.breakdown.values()) == result.score

    def test_clamping_is_the_only_reason_breakdown_exceeds_the_score(self, make_lead):
        result = LeadScorer().score(
            make_lead(
                business_name="Perfect",
                business_type="restaurant",
                city="Aden",
                country="Yemen",
                address="Street",
                phone="+967 1",
                email="a@b.com",
                description="Great business",
                categories=["shop"],
                source="osm",
                social_links={"facebook": "https://facebook.com/x"},
                rating=5.0,
                review_count=500,
                business_status=BusinessStatus.ACTIVE,
                website_status=WebsiteStatus.NOT_FOUND,
            )
        )
        assert sum(result.breakdown.values()) > result.score
        assert result.score == 100

    def test_reasons_are_unique_and_non_empty(self, lead_without_website):
        result = LeadScorer().score(lead_without_website)
        assert result.reasons
        assert len(result.reasons) == len(set(result.reasons))
        assert all(isinstance(reason, str) and reason.strip() for reason in result.reasons)

    def test_every_fired_rule_contributes_a_reason(self, lead_without_website):
        scorer = LeadScorer()
        result = scorer.score(lead_without_website)
        for rule_id in result.breakdown:
            rule = scorer.rules.rule(rule_id)
            if rule.description:
                assert rule.description in result.reasons

    def test_a_record_with_no_signals_gets_an_explicit_reason(self, make_lead):
        result = LeadScorer().score(make_lead(business_name="Nothing"))
        assert result.reasons

    def test_a_phone_reason_appears_only_when_a_phone_exists(self, make_lead):
        scorer = LeadScorer()
        with_phone = scorer.score(
            make_lead(business_name="X", city="A", country="B", phone="+967 1", website_status=WebsiteStatus.NOT_FOUND)
        )
        without_phone = scorer.score(
            make_lead(business_name="X", city="A", country="B", website_status=WebsiteStatus.NOT_FOUND)
        )
        assert "has_phone" in with_phone.breakdown
        assert "has_phone" not in without_phone.breakdown


class TestDataQuality:
    def test_a_near_empty_record_is_flagged_low_quality(self, make_lead):
        lead = make_lead(business_name="Bare", city="", country="", business_type="")
        signals = build_signals(lead)
        assert signals["filled_field_count"] <= 2
        result = LeadScorer().score(lead)
        assert "data_quality_low" in result.breakdown
        assert result.breakdown["data_quality_low"] < 0

    def test_a_well_documented_record_is_not_flagged_low_quality(self, make_lead):
        lead = make_lead(
            business_name="Full",
            city="Aden",
            country="Yemen",
            business_type="restaurant",
            phone="+967 1",
            address="Street",
            email="a@b.com",
            description="Documented",
            categories=["shop"],
        )
        signals = build_signals(lead)
        assert signals["data_quality_low"] is False
        assert "data_quality_low" not in LeadScorer().score(lead).breakdown

    def test_blank_strings_count_as_missing(self, make_lead):
        signals = build_signals(
            make_lead(business_name="X", city="", country="  ", phone="   ", address="")
        )
        assert signals["has_phone"] is False
        assert signals["has_address"] is False
        assert signals["has_location"] is False

    def test_absurd_review_counts_and_ratings_do_not_crash(self, make_lead):
        for review_count, rating in ((-5, 4.5), (0, 0.0), (10**9, 9.9)):
            signals = build_signals(
                make_lead(business_name="X", review_count=review_count, rating=rating)
            )
            assert isinstance(signals["has_reviews"], bool)
            assert isinstance(signals["has_good_rating"], bool)
            assert isinstance(signals["has_many_reviews"], bool)


class TestStoredCheckHydration:
    def test_a_valid_stored_check_is_recovered(self, make_lead):
        stored = WebsiteCheckResult(
            status=WebsiteStatus.NOT_FOUND, quality=WebsiteQuality.UNKNOWN, is_reachable=False
        )
        lead = make_lead(business_name="X", raw={"website_check": stored.to_dict()})
        recovered = hydrate_website_check(lead)
        assert recovered is not None
        assert recovered.status == WebsiteStatus.NOT_FOUND

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"website_check": None},
            {"website_check": {}},
            {"website_check": "not-a-mapping"},
            {"website_check": ["nope"]},
        ],
    )
    def test_missing_or_corrupt_stored_checks_are_discarded(self, make_lead, raw):
        assert hydrate_website_check(make_lead(business_name="X", raw=raw)) is None

    def test_a_missing_raw_payload_is_discarded(self, make_lead):
        lead = make_lead(business_name="X")
        lead.raw = None
        assert hydrate_website_check(lead) is None

    def test_a_corrupt_stored_check_never_yields_website_missing(self, make_lead):
        lead = make_lead(business_name="X", website_status=WebsiteStatus.NOT_CHECKED, raw={"website_check": {"status": "bogus"}})
        assert hydrate_website_check(lead) is None
        result = LeadScorer().score(lead)
        assert "no_website" not in result.breakdown


class TestScorerInterface:
    def test_the_scorer_implements_the_contract(self):
        from lead_finder_agent.scoring.base import BaseLeadScorer

        assert isinstance(LeadScorer(), BaseLeadScorer)

    def test_the_scorer_exposes_the_rules_version(self):
        assert LeadScorer().rules.version >= 1

    def test_scoring_version_is_propagated_to_the_score(self, lead_without_website):
        scorer = LeadScorer()
        assert scorer.score(lead_without_website).scoring_version == scorer.rules.version

    def test_scoring_version_is_propagated_to_the_lead(self, lead_without_website):
        scorer = LeadScorer()
        scorer.score_lead(lead_without_website)
        assert lead_without_website.scoring_version == scorer.rules.version

    def test_a_custom_scorer_can_replace_the_default(self, make_lead):
        from lead_finder_agent.scoring.base import BaseLeadScorer

        class ZeroScorer(BaseLeadScorer):
            def score(self, lead, check=None):
                return LeadScore(score=1, reasons=["custom"], scoring_version=99)

            def score_lead(self, lead, check=None):
                return lead.apply_score(self.score(lead, check))

            def score_all(self, leads, checks=None):
                return [self.score_lead(lead) for lead in leads]

        assert isinstance(ZeroScorer(), BaseLeadScorer)
        lead = ZeroScorer().score_lead(make_lead(business_name="X"))
        assert lead.lead_score == 1
        assert lead.scoring_version == 99


class TestScoreAllPairing:
    def test_score_all_pairs_each_lead_with_its_check(self, make_lead):
        scorer = LeadScorer()
        with_check = make_lead(business_name="A Co", website_status=WebsiteStatus.NOT_CHECKED)
        without_check = make_lead(business_name="B Co", website_status=WebsiteStatus.NOT_CHECKED)
        check = WebsiteCheckResult(status=WebsiteStatus.NOT_FOUND, is_reachable=False)

        scorer.score_all([with_check, without_check], checks={with_check.dedupe_key: check})

        assert "no_website" in with_check.score_breakdown
        assert "no_website" not in without_check.score_breakdown

    def test_score_all_falls_back_to_the_lead_id(self, make_lead):
        scorer = LeadScorer()
        lead = make_lead(business_name="C Co", website_status=WebsiteStatus.NOT_CHECKED)
        check = WebsiteCheckResult(status=WebsiteStatus.NOT_FOUND, is_reachable=False)
        scorer.score_all([lead], checks={lead.id: check})
        assert "no_website" in lead.score_breakdown

    def test_score_all_returns_one_lead_per_input(self, make_lead):
        leads = [make_lead(business_name=f"Biz {i}") for i in range(4)]
        assert len(LeadScorer().score_all(leads)) == 4

    def test_score_lead_returns_the_same_object(self, lead_without_website):
        assert LeadScorer().score_lead(lead_without_website) is lead_without_website


class TestScoringPersistence:
    def test_lead_score_round_trips_through_dict(self, lead_without_website):
        LeadScorer().score_lead(lead_without_website)
        restored = Lead.from_dict(lead_without_website.to_dict())
        assert restored.lead_score == lead_without_website.lead_score
        assert restored.priority == lead_without_website.priority
        assert restored.scoring_version == lead_without_website.scoring_version
        assert restored.score_breakdown == lead_without_website.score_breakdown

    def test_a_lead_score_round_trips_through_dict(self):
        original = LeadScore(
            score=77,
            confidence=Confidence.HIGH,
            reasons=["a reason"],
            priority=LeadPriority.HOT,
            breakdown={"no_website": 35},
            scoring_version=2,
        )
        restored = LeadScore.from_dict(original.to_dict())
        assert restored == original

    def test_a_record_without_a_scoring_version_still_loads(self, lead_without_website):
        payload = lead_without_website.to_dict()
        payload.pop("scoring_version", None)
        assert Lead.from_dict(payload).scoring_version == 1
