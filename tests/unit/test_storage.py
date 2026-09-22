"""Tests for storage, querying and export."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from lead_finder_agent.models import (
    BusinessStatus,
    Confidence,
    Lead,
    LeadPriority,
    WebsiteStatus,
)
from lead_finder_agent.storage import LeadFilter, SQLiteLeadRepository
from lead_finder_agent.storage.exporters import (
    CSV_COLUMNS,
    export_csv,
    export_json,
    export_leads,
    write_csv,
    write_json,
)


@pytest.fixture
def repository() -> SQLiteLeadRepository:
    repo = SQLiteLeadRepository(":memory:")
    yield repo
    repo.close()


def _lead(name: str, **overrides) -> Lead:
    data = {
        "business_name": name,
        "city": "Aden",
        "country": "Yemen",
        "website_status": WebsiteStatus.NOT_FOUND,
        "lead_score": 50,
    }
    data.update(overrides)
    return Lead(**data)


class TestRepositoryCrud:
    def test_add_and_get(self, repository):
        lead = _lead("Aden Traders")
        repository.add(lead)
        fetched = repository.get(lead.id)
        assert fetched is not None
        assert fetched.business_name == "Aden Traders"

    def test_count(self, repository):
        repository.add(_lead("A"))
        repository.add(_lead("B"))
        assert repository.count() == 2
        assert len(repository) == 2

    def test_add_many(self, repository):
        assert repository.add_many([_lead("A"), _lead("B"), _lead("C")]) == 3
        assert repository.count() == 3

    def test_delete(self, repository):
        lead = _lead("Aden Traders")
        repository.add(lead)
        assert repository.delete(lead.id) is True
        assert repository.count() == 0

    def test_delete_missing_returns_false(self, repository):
        assert repository.delete("nope") is False

    def test_clear(self, repository):
        repository.add_many([_lead("A"), _lead("B")])
        repository.clear()
        assert repository.count() == 0

    def test_exists(self, repository):
        lead = _lead("Aden Traders")
        assert repository.exists(lead) is False
        repository.add(lead)
        assert repository.exists(lead) is True


class TestDuplicatePrevention:
    def test_same_business_is_not_duplicated(self, repository):
        repository.add(_lead("Aden Traders", phone="+967 1"))
        repository.add(_lead("Aden Traders", phone="+967 1"))
        assert repository.count() == 1

    def test_readd_refreshes_without_erasing_data(self, repository):
        lead = _lead("Aden Traders", phone="+967 1", address="Main Street")
        repository.add(lead)

        updated = _lead("Aden Traders", phone="+967 1")
        updated.lead_score = 90
        updated.score_confidence = Confidence.HIGH
        updated.priority = LeadPriority.HOT
        repository.add(updated)

        stored = repository.get(lead.id)
        assert repository.count() == 1
        assert stored.lead_score == 90
        assert stored.address == "Main Street"  # not blanked out
        assert stored.phone == "+967 1"

    def test_richer_record_fills_gaps(self, repository):
        repository.add(_lead("Aden Traders", phone="+967 1"))
        repository.add(_lead("Aden Traders", phone="+967 1", email="a@b.com"))
        stored = repository.find(LeadFilter())[0]
        assert stored.email == "a@b.com"

    def test_different_businesses_are_separate(self, repository):
        repository.add(_lead("Aden Traders", phone="+967 1"))
        repository.add(_lead("Sanaa Bakery", phone="+967 2"))
        assert repository.count() == 2


class TestRoundTrip:
    def test_all_fields_survive(self, repository):
        lead = Lead(
            business_name="Aden Traders",
            business_type="shop",
            city="Aden",
            country="Yemen",
            address="Main Street",
            phone="+967 1",
            email="a@b.com",
            source="osm",
            source_url="https://osm.org/node/1",
            website_url="https://shop.example",
            website_status=WebsiteStatus.EXISTS,
            social_links={"facebook": "https://facebook.com/x"},
            description="A shop",
            latitude=12.7,
            longitude=45.0,
            business_status=BusinessStatus.ACTIVE,
            review_count=42,
            rating=4.5,
            categories=["shop", "grocery"],
            lead_score=77,
            score_confidence=Confidence.HIGH,
            score_reason=["Because"],
            score_breakdown={"r": 1},
            priority=LeadPriority.HOT,
        )
        repository.add(lead)
        stored = repository.get(lead.id)

        assert stored.business_type == "shop"
        assert stored.website_status == WebsiteStatus.EXISTS
        assert stored.business_status == BusinessStatus.ACTIVE
        assert stored.social_links == {"facebook": "https://facebook.com/x"}
        assert stored.categories == ["shop", "grocery"]
        assert stored.score_reason == ["Because"]
        assert stored.score_breakdown == {"r": 1}
        assert stored.review_count == 42
        assert stored.rating == pytest.approx(4.5)
        assert stored.priority == LeadPriority.HOT
        assert stored.discovered_at is not None


class TestQueries:
    @pytest.fixture(autouse=True)
    def seeded(self, repository):
        repository.add_many(
            [
                _lead("Aden Cafe", city="Aden", lead_score=90, priority=LeadPriority.HOT, phone="+967 1"),
                _lead("Aden Bakery", city="Aden", lead_score=40, priority=LeadPriority.WARM),
                _lead(
                    "Sanaa Shop",
                    city="Sanaa",
                    lead_score=70,
                    priority=LeadPriority.HOT,
                    website_status=WebsiteStatus.EXISTS,
                    phone="+967 2",
                ),
            ]
        )

    def test_filter_by_city_case_insensitively(self, repository):
        assert len(repository.find(LeadFilter(city="aden"))) == 2

    def test_filter_by_website_status(self, repository):
        found = repository.find(LeadFilter(website_status=WebsiteStatus.EXISTS))
        assert len(found) == 1
        assert found[0].business_name == "Sanaa Shop"

    def test_filter_by_min_score(self, repository):
        found = repository.find(LeadFilter(min_score=70))
        assert len(found) == 2

    def test_filter_by_priority(self, repository):
        found = repository.find(LeadFilter(priority="hot"))
        assert len(found) == 2

    def test_filter_by_has_phone(self, repository):
        assert len(repository.find(LeadFilter(has_phone=True))) == 2
        assert len(repository.find(LeadFilter(has_phone=False))) == 1

    def test_default_order_is_score_descending(self, repository):
        scores = [lead.lead_score for lead in repository.find(LeadFilter())]
        assert scores == sorted(scores, reverse=True)

    def test_order_asending(self, repository):
        scores = [lead.lead_score for lead in repository.find(LeadFilter(descending=False))]
        assert scores == sorted(scores)

    def test_limit_and_offset(self, repository):
        first = repository.find(LeadFilter(limit=1))
        second = repository.find(LeadFilter(limit=1, offset=1))
        assert first[0].id != second[0].id

    def test_top_returns_highest_scores(self, repository):
        top = repository.top(2)
        assert [lead.lead_score for lead in top] == [90, 70]

    def test_invalid_order_by_falls_back_safely(self, repository):
        found = repository.find(LeadFilter(order_by="lead_score; DROP TABLE leads"))
        assert len(found) == 3  # table still there, query still ordered

    def test_stats(self, repository):
        stats = repository.stats()
        assert stats["total"] == 3
        assert stats["confirmed_no_website"] == 2
        assert stats["with_website"] == 1


class TestPersistence:
    def test_data_survives_reopening(self, tmp_path: Path):
        path = tmp_path / "leads.db"
        repo = SQLiteLeadRepository(path)
        repo.add(_lead("Durable Traders", phone="+967 1"))
        repo.close()

        reopened = SQLiteLeadRepository(path)
        assert reopened.count() == 1
        assert reopened.find()[0].business_name == "Durable Traders"
        reopened.close()

    def test_repository_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "nested" / "deep" / "leads.db"
        repo = SQLiteLeadRepository(path)
        repo.close()
        assert path.exists()


class TestExport:
    @pytest.fixture
    def leads(self):
        return [
            _lead(
                "Aden Cafe",
                phone="+967 1",
                email="cafe@example.com",
                social_links={"facebook": "https://facebook.com/cafe"},
                lead_score=90,
                score_reason=["No website"],
                priority=LeadPriority.HOT,
            ),
            _lead("Aden Bakery", lead_score=40),
        ]

    def test_export_json_is_valid(self, leads):
        payload = json.loads(export_json(leads))
        assert isinstance(payload, list)
        assert payload[0]["business_name"] == "Aden Cafe"
        assert len(payload) == 2

    def test_export_json_with_metadata(self, leads):
        payload = json.loads(export_json(leads, metadata={"agent": "lead_finder"}))
        assert payload["metadata"]["count"] == 2
        assert payload["metadata"]["agent"] == "lead_finder"

    def test_export_csv_has_header_and_rows(self, leads):
        text = export_csv(leads)
        rows = list(csv.reader(text.splitlines()))
        assert rows[0] == list(CSV_COLUMNS)
        assert len(rows) == 3  # header + 2

    def test_export_csv_flattens_nested_fields(self, leads):
        text = export_csv(leads)
        assert "facebook: https://facebook.com/cafe" in text
        assert "No website" in text

    def test_export_csv_is_quote_safe(self):
        lead = _lead("Cafe, Bakery & Bar", description='He said "hi"')
        text = export_csv([lead])
        rows = list(csv.reader(text.splitlines()))
        assert rows[1][1] == "Cafe, Bakery & Bar"

    def test_write_json_creates_file_and_dirs(self, leads, tmp_path: Path):
        target = tmp_path / "out" / "leads.json"
        write_json(leads, target)
        assert target.exists()
        assert len(json.loads(target.read_text())) == 2

    def test_write_csv_creates_file(self, leads, tmp_path: Path):
        target = tmp_path / "leads.csv"
        write_csv(leads, target)
        assert target.exists()

    def test_export_leads_infers_format_from_extension(self, leads, tmp_path: Path):
        json_path = export_leads(leads, tmp_path / "a.json")
        csv_path = export_leads(leads, tmp_path / "a.csv")
        assert json_path.exists() and csv_path.exists()

    def test_export_leads_honours_explicit_format(self, leads, tmp_path: Path):
        target = tmp_path / "data.txt"
        export_leads(leads, target, fmt="csv")
        assert target.read_text().startswith("id,business_name")

    def test_export_leads_rejects_unknown_format(self, leads, tmp_path: Path):
        with pytest.raises(ValueError):
            export_leads(leads, tmp_path / "x.xml", fmt="xml")

    def test_export_empty_list(self):
        assert json.loads(export_json([])) == []
        assert export_csv([]).strip() == ",".join(CSV_COLUMNS)
