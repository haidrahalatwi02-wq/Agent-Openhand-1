"""Tests for the website checker."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lead_finder_agent.checker import HttpWebsiteChecker, WebsiteSignals, website_from_tags
from lead_finder_agent.checker.osm_tags import is_social_url
from lead_finder_agent.models import WebsiteQuality, WebsiteStatus

from tests.conftest import FakeTransport

CHECKED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def checker_factory(fake_transport: FakeTransport):
    def _factory(**kwargs):
        kwargs.setdefault("probe_by_name", False)
        return HttpWebsiteChecker(
            client=fake_transport.client(), now=CHECKED_AT, **kwargs
        )

    return _factory


class TestWebsiteFromTags:
    def test_splits_website_and_social(self):
        website, social = website_from_tags(
            {"website": "https://shop.example", "contact:facebook": "https://facebook.com/shop"}
        )
        assert website == "https://shop.example"
        assert social["facebook"] == "https://facebook.com/shop"

    def test_social_url_in_website_tag_is_not_a_website(self):
        website, social = website_from_tags({"website": "https://facebook.com/shop"})
        assert website is None
        assert social["facebook"] == "https://facebook.com/shop"

    @pytest.mark.parametrize(
        "url",
        [
            "https://facebook.com/x",
            "https://instagram.com/x",
            "https://wa.me/967123",
            "https://t.me/x",
            "https://x.com/x",
        ],
    )
    def test_is_social_url_detects_platforms(self, url):
        assert is_social_url(url) is True

    def test_is_social_url_false_for_owned_site(self):
        assert is_social_url("https://myshop.example") is False
        assert is_social_url(None) is False


class TestCheckerStatuses:
    def test_reachable_good_site(self, fake_transport, checker_factory, good_page_html):
        fake_transport.add("shop.example", status=200, body=good_page_html)
        result = checker_factory().check(
            __import__("lead_finder_agent.models", fromlist=["Lead"]).Lead(
                business_name="Shop", website_url="https://shop.example"
            )
        )
        assert result.status == WebsiteStatus.EXISTS
        assert result.quality == WebsiteQuality.GOOD
        assert result.is_reachable is True
        assert result.has_shop is True
        assert result.has_contact_page is True
        assert result.has_https is True

    def test_404_gives_not_found(self, fake_transport, checker_factory):
        fake_transport.add("gone.example", status=404, body="not found")
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://gone.example"))
        assert result.status == WebsiteStatus.NOT_FOUND
        assert result.http_status == 404

    def test_410_gives_not_found(self, fake_transport, checker_factory):
        fake_transport.add("gone.example", status=410, body="")
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://gone.example"))
        assert result.status == WebsiteStatus.NOT_FOUND

    def test_network_error_gives_unreachable(self, checker_factory):
        from lead_finder_agent.models import Lead

        result = checker_factory().check(
            Lead(business_name="X", website_url="https://unreachable.example")
        )
        assert result.status == WebsiteStatus.UNREACHABLE
        assert result.error is not None

    def test_server_error_is_weak_not_missing(self, fake_transport, checker_factory):
        fake_transport.add("flaky.example", status=503, body="")
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://flaky.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.quality == WebsiteQuality.WEAK

    def test_no_url_gives_unknown(self, checker_factory):
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="No Website Biz"))
        assert result.status == WebsiteStatus.UNKNOWN
        assert result.quality == WebsiteQuality.UNKNOWN

    def test_social_url_gives_exists_but_social_only(self, checker_factory):
        from lead_finder_agent.models import Lead

        result = checker_factory().check(
            Lead(business_name="X", website_url="https://facebook.com/x")
        )
        assert result.status == WebsiteStatus.EXISTS
        assert result.quality == WebsiteQuality.SOCIAL_ONLY
        assert result.social_only is True

    def test_guessed_domain_404_stays_unknown(self, fake_transport):
        """A guessed domain must never prove the business has no website."""
        fake_transport.add(".com", status=404, body="")
        checker = HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=True, now=CHECKED_AT)
        from lead_finder_agent.models import Lead

        result = checker.check(Lead(business_name="Some Unknown Shop"))
        assert result.status == WebsiteStatus.UNKNOWN

    def test_guessed_domain_hit_is_found(self, fake_transport, good_page_html):
        fake_transport.add("adentraders.com", status=200, body=good_page_html)
        checker = HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=True, now=CHECKED_AT)
        from lead_finder_agent.models import Lead

        result = checker.check(Lead(business_name="Aden Traders"))
        assert result.status == WebsiteStatus.EXISTS


class TestQualityClassification:
    def test_placeholder_page_is_weak(self, fake_transport, checker_factory, placeholder_html):
        fake_transport.add("parked.example", status=200, body=placeholder_html)
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://parked.example"))
        assert result.quality == WebsiteQuality.WEAK
        assert any("placeholder" in note.lower() for note in result.notes)

    def test_thin_page_is_weak(self, fake_transport, checker_factory, thin_page_html):
        fake_transport.add("thin.example", status=200, body=thin_page_html)
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://thin.example"))
        assert result.quality == WebsiteQuality.WEAK

    def test_outdated_page_is_weak(self, fake_transport, checker_factory, outdated_html):
        fake_transport.add("old.example", status=200, body=outdated_html)
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://old.example"))
        assert result.quality == WebsiteQuality.WEAK
        assert any("outdated" in note.lower() for note in result.notes)

    def test_free_builder_host_is_weak(self, fake_transport, checker_factory, good_page_html):
        fake_transport.add("wixsite.com", status=200, body=good_page_html)
        from lead_finder_agent.models import Lead

        result = checker_factory().check(
            Lead(business_name="X", website_url="https://shop.wixsite.com/home")
        )
        assert result.quality == WebsiteQuality.WEAK

    def test_http_only_site_is_reachable_without_https(self, fake_transport, checker_factory, good_page_html):
        def handler(method, url, params, data, headers, timeout):
            from lead_finder_agent.utils.http import HttpResponse

            if url.startswith("https://"):
                return HttpResponse(0, "", {}, url, error="tls failed")
            return HttpResponse(200, good_page_html, {}, url)

        fake_transport.add_handler("plain.example", handler)
        from lead_finder_agent.models import Lead

        result = checker_factory().check(Lead(business_name="X", website_url="https://plain.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.has_https is False


class TestContactPageProbe:
    def test_contact_page_sets_flag(self, fake_transport, good_page_html):
        from lead_finder_agent.models import Lead

        def handler(method, url, params, data, headers, timeout):
            from lead_finder_agent.utils.http import HttpResponse

            if url.endswith("/contact"):
                return HttpResponse(200, "<html><body>Contact</body></html>", {}, url)
            return HttpResponse(200, "<html><body>" + "content " * 200 + "</body></html>", {}, url)

        fake_transport.add_handler("probe.example", handler)
        checker = HttpWebsiteChecker(
            client=fake_transport.client(),
            probe_by_name=False,
            probe_contact_page=True,
            now=CHECKED_AT,
        )
        result = checker.check(Lead(business_name="X", website_url="https://probe.example"))
        assert result.has_contact_page is True


class TestCheckMany:
    def test_checks_every_lead(self, fake_transport, good_page_html):
        from lead_finder_agent.models import Lead

        fake_transport.add("ok.example", status=200, body=good_page_html)
        checker = HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False, now=CHECKED_AT)
        leads = [Lead(business_name=f"Biz {i}", website_url="https://ok.example") for i in range(3)]
        results = checker.check_many(leads)
        assert len(results) == 3

    def test_respects_limit(self, fake_transport, good_page_html):
        from lead_finder_agent.models import Lead

        fake_transport.add("ok.example", status=200, body=good_page_html)
        checker = HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False, now=CHECKED_AT)
        leads = [Lead(business_name=f"Biz {i}", website_url="https://ok.example") for i in range(5)]
        assert len(checker.check_many(leads, limit=2)) == 2

    def test_exception_in_one_lead_does_not_abort(self, monkeypatch):
        from lead_finder_agent.models import Lead

        checker = HttpWebsiteChecker(probe_by_name=False, now=CHECKED_AT)
        calls = {"n": 0}

        def flaky(lead):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            from lead_finder_agent.models import WebsiteCheckResult

            return WebsiteCheckResult(status=WebsiteStatus.UNKNOWN)

        monkeypatch.setattr(checker, "check", flaky)
        results = checker.check_many([Lead(business_name="A"), Lead(business_name="B")])
        assert len(results) == 2
        assert results[0].error is not None


class TestSignalsConfig:
    def test_signals_load_from_packaged_data(self):
        signals = WebsiteSignals.load()
        assert "facebook.com" in signals.social_domains
        assert signals.min_content_length > 0
        assert 200 in signals.ok_status

    def test_signals_accept_overrides(self):
        signals = WebsiteSignals.load({"min_content_length": 9999})
        assert signals.min_content_length == 9999
