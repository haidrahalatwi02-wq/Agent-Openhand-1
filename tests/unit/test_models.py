"""Tests for the Lead data model."""

from __future__ import annotations

from datetime import timezone

from lead_finder_agent.models import (
    BusinessStatus,
    Confidence,
    Lead,
    LeadScore,
    WebsiteStatus,
    dedupe_leads,
    normalize_name,
    normalize_phone,
    normalize_url,
    parse_datetime,
    utcnow,
)


class TestNormalizers:
    def test_normalize_name_strips_punctuation_and_case(self):
        assert normalize_name("Al-Bahr  Seafood, Restaurant!") == "al-bahr seafood restaurant"

    def test_normalize_name_handles_arabic(self):
        assert normalize_name("مطعم البحر") == "مطعم البحر"

    def test_normalize_name_empty(self):
        assert normalize_name(None) == ""
        assert normalize_name("   ") == ""

    def test_normalize_phone_keeps_digits_only(self):
        assert normalize_phone("+967 71 234-5678") == "967712345678"
        assert normalize_phone("(02) 333 444") == "02333444"
        assert normalize_phone(None) == ""

    def test_normalize_url_adds_scheme_and_trims(self):
        assert normalize_url("example.com/") == "https://example.com"
        assert normalize_url("http://example.com/path/") == "http://example.com/path"
        assert normalize_url("") is None
        assert normalize_url(None) is None


class TestTimestamps:
    def test_utcnow_is_timezone_aware(self):
        assert utcnow().tzinfo == timezone.utc

    def test_parse_datetime_accepts_iso_and_z(self):
        assert parse_datetime("2026-01-01T00:00:00Z").year == 2026
        assert parse_datetime("2026-01-01T00:00:00+00:00").month == 1
        assert parse_datetime("not a date") is None
        assert parse_datetime(None) is None


class TestLead:
    def test_dedupe_key_prefers_source_id(self, make_lead):
        lead = make_lead(raw={"source_id": "node/1"}, source="osm")
        assert lead.compute_dedupe_key() == Lead(
            business_name="Test Business", raw={"source_id": "node/1"}, source="osm"
        ).dedupe_key

    def test_dedupe_key_falls_back_to_name_city_phone(self, make_lead):
        a = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        b = make_lead(business_name="cafe!", city="aden", phone="9671")
        assert a.dedupe_key == b.dedupe_key

    def test_id_defaults_to_dedupe_key(self, make_lead):
        lead = make_lead()
        assert lead.id == lead.dedupe_key
        assert lead.id

    def test_apply_score_copies_fields(self, make_lead):
        lead = make_lead()
        lead.apply_score(
            LeadScore(
                score=80,
                confidence=Confidence.HIGH,
                reasons=["r1"],
                priority=__import__(
                    "lead_finder_agent.models", fromlist=["LeadPriority"]
                ).LeadPriority.HOT,
                breakdown={"r": 80},
            )
        )
        assert lead.lead_score == 80
        assert lead.score_confidence == Confidence.HIGH
        assert lead.score_reason == ["r1"]
        assert lead.score_breakdown == {"r": 80}

    def test_round_trip_to_dict_from_dict(self, lead_without_website):
        data = lead_without_website.to_dict()
        assert data["website_status"] == "website_not_found"
        assert isinstance(data["discovered_at"], str)
        restored = Lead.from_dict(data)
        assert restored.business_name == lead_without_website.business_name
        assert restored.website_status == WebsiteStatus.NOT_FOUND
        assert restored.business_status == BusinessStatus.ACTIVE
        assert restored.dedupe_key == lead_without_website.dedupe_key

    def test_confidence_rank_orders_values(self, make_lead):
        low = make_lead(score_confidence=Confidence.LOW)
        high = make_lead(score_confidence=Confidence.HIGH)
        assert high.confidence_rank() > low.confidence_rank()

    def test_has_website_property(self, lead_with_good_website, lead_without_website):
        assert lead_with_good_website.has_website is True
        assert lead_without_website.has_website is False

    def test_summary_row_has_expected_columns(self, lead_without_website):
        row = lead_without_website.summary_row()
        assert len(row) == 7
        assert row[0] == "Aden Traders"


class TestDedupeLeads:
    def test_identical_leads_collapse(self, make_lead):
        a = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        b = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        assert len(dedupe_leads([a, b])) == 1

    def test_distinct_leads_are_kept(self, make_lead):
        a = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        b = make_lead(business_name="Bakery", city="Aden", phone="+967 2")
        assert len(dedupe_leads([a, b])) == 2

    def test_merge_fills_missing_fields(self, make_lead):
        sparse = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        rich = make_lead(
            business_name="Cafe",
            city="Aden",
            phone="+967 1",
            email="cafe@example.com",
            address="Main Street",
            description="A nice cafe",
        )
        merged = dedupe_leads([sparse, rich])
        assert len(merged) == 1
        assert merged[0].email == "cafe@example.com"
        assert merged[0].address == "Main Street"
