"""Tests for data normalization and de-duplication."""

from __future__ import annotations

import pytest

from lead_finder_agent.extraction import Deduplicator, LeadNormalizer, deduplicate, normalize_lead
from lead_finder_agent.models import BusinessStatus


class TestLeadNormalizer:
    def test_maps_common_field_aliases(self):
        raw = {
            "name": "  Al Bahr  Restaurant ",
            "category": "restaurant",
            "street": "",
            "phone": "+967 71 234 5678",
            "lat": "12.78",
            "lon": "45.01",
            "reviews": "120",
            "rating": "4.4",
        }
        lead = normalize_lead(raw, source="test")
        assert lead.business_name == "Al Bahr Restaurant"
        assert lead.business_type == "restaurant"
        assert lead.source == "test"
        assert lead.latitude == pytest.approx(12.78)
        assert lead.review_count == 120
        assert lead.rating == pytest.approx(4.4)

    def test_rejects_record_without_name(self):
        assert normalize_lead({"phone": "123"}) is None

    def test_rejects_non_mapping(self):
        assert normalize_lead("not a dict") is None  # type: ignore[arg-type]

    def test_fills_default_location(self):
        normalizer = LeadNormalizer(default_country="Yemen", default_city="Aden")
        lead = normalizer.normalize({"business_name": "Shop"})
        assert lead.country == "Yemen"
        assert lead.city == "Aden"

    def test_cleans_phone_and_email(self):
        lead = normalize_lead(
            {
                "business_name": "Shop",
                "phone": "tel: +967 71 234 5678 ext",
                "email": "INFO@Example.COM",
            }
        )
        assert lead.phone.startswith("+967")
        assert lead.email == "info@example.com"

    def test_rejects_too_short_phone(self):
        lead = normalize_lead({"business_name": "Shop", "phone": "12"})
        assert lead.phone is None

    def test_normalizes_urls(self):
        lead = normalize_lead({"business_name": "Shop", "website_url": "example.com/"})
        assert lead.website_url == "https://example.com"

    def test_social_links_cleaned_from_list(self):
        lead = normalize_lead(
            {
                "business_name": "Shop",
                "social_links": ["https://facebook.com/shop", "https://instagram.com/shop"],
            }
        )
        assert set(lead.social_links) == {"facebook", "instagram"}

    def test_drops_forbidden_personal_fields(self):
        lead = normalize_lead(
            {"business_name": "Shop", "national_id": "123", "passport": "X", "credit_card": "4111"}
        )
        assert "national_id" not in lead.raw
        assert "passport" not in lead.raw
        assert "credit_card" not in lead.raw

    def test_description_is_truncated(self):
        lead = normalize_lead({"business_name": "Shop", "description": "x" * 2000})
        assert len(lead.description) <= 500

    def test_business_status_mapping(self):
        assert normalize_lead({"business_name": "S", "business_status": "active"}).business_status == BusinessStatus.ACTIVE
        assert normalize_lead({"business_name": "S", "business_status": "closed"}).business_status == BusinessStatus.CLOSED
        assert normalize_lead({"business_name": "S", "business_status": "??"}).business_status == BusinessStatus.UNKNOWN

    def test_categories_deduplicated_and_split(self):
        lead = normalize_lead({"business_name": "Shop", "categories": "shop, bakery, shop"})
        assert lead.categories == ["shop", "bakery"]

    def test_raw_payload_is_json_safe(self):
        class Custom:
            def __str__(self):
                return "custom"

        lead = normalize_lead({"business_name": "Shop", "extra": Custom()})
        assert lead.raw["extra"] == "custom"


class TestDeduplicator:
    def test_exact_duplicates_collapse(self, make_lead):
        a = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        b = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        dedup = Deduplicator()
        assert len(dedup.deduplicate([a, b])) == 1
        assert dedup.merged_count == 1

    def test_fuzzy_match_on_similar_name_and_city(self, make_lead):
        a = make_lead(business_name="Al Bahr Seafood Restaurant", city="Aden")
        b = make_lead(business_name="Al-Bahr Seafood Restaurants", city="Aden")
        result = Deduplicator(fuzzy=True).deduplicate([a, b])
        assert len(result) == 1

    def test_fuzzy_disabled_keeps_both(self, make_lead):
        a = make_lead(business_name="Al Bahr Seafood Restaurant", city="Aden")
        b = make_lead(business_name="Al-Bahr Seafood Restaurants", city="Aden")
        assert len(Deduplicator(fuzzy=False).deduplicate([a, b])) == 2

    def test_same_name_different_city_stays_separate(self, make_lead):
        a = make_lead(business_name="City Cafe", city="Aden")
        b = make_lead(business_name="City Cafe", city="Sanaa")
        assert len(Deduplicator().deduplicate([a, b])) == 2

    def test_matching_phone_merges_regardless_of_name(self, make_lead):
        a = make_lead(business_name="Aden Traders", city="Aden", phone="+967 71 000 1111")
        b = make_lead(business_name="Aden Trading Co", city="Aden", phone="967710001111")
        assert len(Deduplicator().deduplicate([a, b])) == 1

    def test_merge_keeps_richest_data(self, make_lead):
        sparse = make_lead(business_name="Cafe", city="Aden", phone="+967 1")
        rich = make_lead(
            business_name="Cafe",
            city="Aden",
            phone="+967 1",
            email="c@example.com",
            description="Nice cafe",
        )
        merged = deduplicate([sparse, rich])
        assert merged[0].email == "c@example.com"

    def test_empty_input(self):
        assert Deduplicator().deduplicate([]) == []

    def test_distinct_businesses_preserved(self, make_lead):
        leads = [
            make_lead(business_name="Alpha Shop", city="Aden", phone="+967 1"),
            make_lead(business_name="Beta Bakery", city="Aden", phone="+967 2"),
            make_lead(business_name="Gamma Cafe", city="Sanaa", phone="+967 3"),
        ]
        assert len(Deduplicator().deduplicate(leads)) == 3
