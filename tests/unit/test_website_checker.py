"""Website checker tests.

Every external request is faked through ``FakeTransport``; nothing here touches
the network. The point of the suite is not only that a good site is detected, but
that an *inconclusive* outcome is never reported as "this business has no
website" - that is the rule the whole design is built around.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from lead_finder_agent.checker import (
    HttpWebsiteChecker,
    available_checkers,
    create_checker,
    register_checker,
    validate_website_url,
)
from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.checker.errors import classify_error, humanize_error
from lead_finder_agent.models import (
    Lead,
    WebsiteCheckResult,
    WebsiteErrorKind,
    WebsiteIdentity,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.utils.http import HttpClient, HttpResponse
from tests.conftest import FakeTransport

CHECKED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

GOOD_HTML = (
    "<html><head><title>Aden Traders</title></head><body>"
    + ("Welcome to Aden Traders. We sell quality goods. " * 20)
    + '<a href="/contact">Contact us</a><footer>&copy; 2026 Aden Traders</footer>'
    "</body></html>"
)


def _checker(transport: FakeTransport, **kwargs: Any) -> HttpWebsiteChecker:
    kwargs.setdefault("probe_by_name", False)
    kwargs.setdefault("now", CHECKED_AT)
    return HttpWebsiteChecker(client=transport.client(), **kwargs)


def _lead(url: Optional[str] = None, *, source: Optional[str] = None, **kw: Any) -> Lead:
    return Lead(business_name="Aden Traders", city="Aden", website_url=url, source=source, **kw)


def _redirect(location: str, status: int = 302):
    def handler(method, url, params, data, headers, timeout) -> HttpResponse:
        return HttpResponse(status, "", {"location": location}, url)

    return handler


# --------------------------------------------------------------------------- #
# Interface and replaceability
# --------------------------------------------------------------------------- #


class TestCheckerInterface:
    def test_checks_a_business_and_returns_structured_result(self):
        transport = FakeTransport().add("ok.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://ok.example"))
        assert isinstance(result, WebsiteCheckResult)
        assert result.status == WebsiteStatus.EXISTS
        assert result.http_status == 200

    def test_registry_exposes_the_http_checker(self):
        assert "http" in available_checkers()
        assert isinstance(create_checker("http"), BaseWebsiteChecker)

    def test_registry_builds_a_checker_from_config(self):
        checker = create_checker("http", {"probe_by_name": False, "max_redirects": 2})
        assert isinstance(checker, HttpWebsiteChecker)
        assert checker.max_redirects == 2

    def test_registry_rejects_unknown_name(self):
        with pytest.raises(KeyError):
            create_checker("does-not-exist")

    def test_registry_allows_a_new_strategy_to_be_added(self):
        class Dummy(BaseWebsiteChecker):
            name = "dummy"

            def check(self, lead):
                return WebsiteCheckResult(status=WebsiteStatus.UNKNOWN)

        register_checker("dummy-test", lambda config: Dummy())
        try:
            assert "dummy-test" in available_checkers()
            assert isinstance(create_checker("dummy-test"), Dummy)
        finally:
            from lead_finder_agent.checker import registry

            registry._REGISTRY.pop("dummy-test", None)

    def test_registry_refuses_silent_overwrite(self):
        with pytest.raises(ValueError):
            register_checker("http", lambda config: None)

    def test_check_many_isolates_one_failure(self, monkeypatch):
        checker = _checker(FakeTransport())
        calls = {"n": 0}

        def flaky(lead):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return WebsiteCheckResult(status=WebsiteStatus.UNKNOWN)

        monkeypatch.setattr(checker, "check", flaky)
        results = checker.check_many([_lead("https://a.example"), _lead("https://b.example")])
        assert len(results) == 2
        assert results[0].status == WebsiteStatus.UNKNOWN
        assert results[0].error is not None


# --------------------------------------------------------------------------- #
# URL normalization and validation
# --------------------------------------------------------------------------- #


class TestUrlNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("example.com", "https://example.com"),
            ("example.com/", "https://example.com"),
            ("http://example.com/path/", "http://example.com/path"),
            ("https://example.com", "https://example.com"),
            ("  example.com  ", "https://example.com"),
            ("www.example.co.uk", "https://www.example.co.uk"),
            ("https:example.com", "https://example.com"),
            ("example.com:8080/x", "https://example.com:8080/x"),
        ],
    )
    def test_normalizes_safe_forms(self, raw, expected):
        assert validate_website_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,<h1>x</h1>",
            "ftp://example.com",
            "mailto:a@b.com",
            "tel:+967123",
            "https://user:pass@example.com",
            "call us",
            "N/A",
            "-",
            "",
            None,
            "example",
            "999.999.999.999",
            "https://" + "a" * 3000 + ".com",
        ],
    )
    def test_rejects_unsafe_or_malformed_values(self, raw):
        assert validate_website_url(raw) is None

    def test_missing_scheme_is_upgraded_not_invented(self):
        # A real host is completed to HTTPS...
        assert validate_website_url("aden-traders.com") == "https://aden-traders.com"
        # ...but arbitrary text is not turned into a website at all.
        assert validate_website_url("Aden Traders Restaurant") is None

    def test_invalid_url_from_provider_is_unknown_not_not_found(self):
        transport = FakeTransport()
        result = _checker(transport).check(_lead("N/A", source="google_places"))
        assert result.status == WebsiteStatus.UNKNOWN
        assert result.error_kind == WebsiteErrorKind.INVALID_URL
        assert transport.requests == []


# --------------------------------------------------------------------------- #
# HTTP statuses
# --------------------------------------------------------------------------- #


class TestHttpVerification:
    def test_valid_https_website(self):
        transport = FakeTransport().add("ok.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://ok.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.is_reachable is True
        assert result.has_https is True
        assert result.quality == WebsiteQuality.GOOD

    def test_valid_http_website_is_reachable(self):
        def handler(method, url, params, data, headers, timeout):
            if url.startswith("https://"):
                return HttpResponse(0, "", {}, url, error="connection refused")
            return HttpResponse(200, GOOD_HTML, {}, url)

        transport = FakeTransport()
        transport.add_handler("plain.example", handler)
        result = _checker(transport).check(_lead("http://plain.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.has_https is False

    @pytest.mark.parametrize("code", [404, 410])
    def test_not_found_statuses(self, code):
        transport = FakeTransport().add("gone.example", status=code)
        result = _checker(transport).check(_lead("https://gone.example"))
        assert result.status == WebsiteStatus.NOT_FOUND

    @pytest.mark.parametrize("code", [403, 429])
    def test_blocked_or_limited_is_weak_not_missing(self, code):
        transport = FakeTransport().add("blocked.example", status=code)
        result = _checker(transport).check(_lead("https://blocked.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.quality == WebsiteQuality.WEAK

    @pytest.mark.parametrize("code", [500, 503])
    def test_server_error_is_weak_not_missing(self, code):
        transport = FakeTransport().add("broken.example", status=code)
        result = _checker(transport).check(_lead("https://broken.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.quality == WebsiteQuality.WEAK

    def test_empty_response_body_still_counts_as_reachable(self):
        transport = FakeTransport().add("empty.example", status=200, body="")
        result = _checker(transport).check(_lead("https://empty.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.page_title is None

    def test_malformed_body_does_not_break_the_check(self):
        transport = FakeTransport().add(
            "malformed.example", status=200, body="<html><title>unclosed"
        )
        result = _checker(transport).check(_lead("https://malformed.example"))
        assert result.status == WebsiteStatus.EXISTS


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #


class TestTransportFailures:
    def _failing(self, message: str) -> HttpWebsiteChecker:
        transport = FakeTransport()

        def handler(method, url, params, data, headers, timeout):
            return HttpResponse(0, "", {}, url, error=message)

        transport.add_handler("fail.example", handler)
        return _checker(transport)

    @pytest.mark.parametrize(
        "message,kind",
        [
            ("connection timed out", WebsiteErrorKind.TIMEOUT),
            ("Read timed out.", WebsiteErrorKind.TIMEOUT),
            ("[Errno -2] Name or service not known", WebsiteErrorKind.DNS_FAILURE),
            ("getaddrinfo failed", WebsiteErrorKind.DNS_FAILURE),
            ("SSLError: certificate verify failed", WebsiteErrorKind.TLS_FAILURE),
            ("[SSL: CERTIFICATE_VERIFY_FAILED]", WebsiteErrorKind.TLS_FAILURE),
            ("Connection refused", WebsiteErrorKind.CONNECTION_FAILURE),
            ("connection reset by peer", WebsiteErrorKind.CONNECTION_FAILURE),
        ],
    )
    def test_transport_failures_are_unreachable_and_classified(self, message, kind):
        result = self._failing(message).check(_lead("https://fail.example"))
        assert result.status == WebsiteStatus.UNREACHABLE
        assert result.error_kind == kind
        assert result.is_reachable is False

    def test_classifier_never_raises_on_unknown_text(self):
        assert classify_error("something bizarre") == WebsiteErrorKind.UNKNOWN
        assert classify_error(None) == WebsiteErrorKind.UNKNOWN

    def test_classifier_uses_status_when_there_is_no_message(self):
        assert classify_error(None, 500) == WebsiteErrorKind.HTTP_ERROR

    def test_human_phrasing_never_contains_raw_exception_text(self):
        phrase = humanize_error(WebsiteErrorKind.TLS_FAILURE)
        assert "SSL" not in phrase
        assert "certificate" not in phrase.lower()


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #


class TestRedirects:
    def test_follows_a_redirect_and_records_the_final_url(self):
        transport = FakeTransport()
        transport.add_handler("start.example", _redirect("https://final.example/page", 301))
        transport.add("final.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://start.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.final_url == "https://final.example/page"

    def test_redirect_loop_is_unreachable_not_a_hang(self):
        transport = FakeTransport()
        transport.add_handler("loop.example", _redirect("https://loop.example/b"))
        result = _checker(transport, max_redirects=10).check(_lead("https://loop.example"))
        assert result.status == WebsiteStatus.UNREACHABLE
        assert result.error_kind == WebsiteErrorKind.REDIRECT_LIMIT

    def test_excessive_redirects_stop_at_the_limit(self):
        counter = {"n": 0}

        def chain(method, url, params, data, headers, timeout):
            counter["n"] += 1
            return HttpResponse(302, "", {"location": f"https://hop.example/{counter['n']}"}, url)

        transport = FakeTransport()
        transport.add_handler("hop.example", chain)
        result = _checker(transport, max_redirects=3).check(_lead("https://hop.example"))
        assert result.status == WebsiteStatus.UNREACHABLE
        assert result.error_kind == WebsiteErrorKind.REDIRECT_LIMIT
        # The limit is honoured: at most limit+1 requests are made.
        assert len(transport.requests) <= 4

    def test_redirect_without_location_is_an_error(self):
        transport = FakeTransport().add("bare.example", status=302, body="")
        result = _checker(transport).check(_lead("https://bare.example"))
        assert result.status == WebsiteStatus.UNREACHABLE
        assert result.error_kind == WebsiteErrorKind.REDIRECT_LIMIT


# --------------------------------------------------------------------------- #
# Size protection
# --------------------------------------------------------------------------- #


class TestResponseSizeLimit:
    def test_large_response_is_truncated_not_downloaded_whole(self):
        transport = FakeTransport().add("big.example", status=200, body="y" * 5000)
        result = _checker(transport, max_response_size=1000).check(_lead("https://big.example"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.truncated is True
        assert any("truncated" in note.lower() for note in result.notes)

    def test_body_within_limit_is_not_flagged(self):
        transport = FakeTransport().add("small.example", status=200, body="z" * 100)
        result = _checker(transport, max_response_size=1000).check(_lead("https://small.example"))
        assert result.truncated is False


# --------------------------------------------------------------------------- #
# Website source handling
# --------------------------------------------------------------------------- #


class TestWebsiteSources:
    def test_missing_website_does_not_become_not_found(self):
        transport = FakeTransport()
        result = _checker(transport).check(_lead(None))
        assert result.status == WebsiteStatus.UNKNOWN
        assert result.status != WebsiteStatus.NOT_FOUND
        assert transport.requests == []

    def test_missing_website_does_not_become_not_found_from_google(self):
        transport = FakeTransport()
        result = _checker(transport).check(_lead(None, source="google_places"))
        assert result.status == WebsiteStatus.UNKNOWN

    def test_missing_website_does_not_become_not_found_from_osm(self):
        transport = FakeTransport()
        result = _checker(transport).check(_lead(None, source="osm"))
        assert result.status == WebsiteStatus.UNKNOWN

    def test_website_url_from_google_places_is_used_and_traced(self):
        transport = FakeTransport().add("from-google.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(
            _lead("https://from-google.example", source="google_places")
        )
        assert result.status == WebsiteStatus.EXISTS
        assert result.website_source == "google_places"

    def test_website_url_from_osm_is_used_and_traced(self):
        transport = FakeTransport().add("from-osm.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://from-osm.example", source="osm"))
        assert result.status == WebsiteStatus.EXISTS
        assert result.website_source == "osm"

    def test_social_profile_is_not_treated_as_an_owned_website(self):
        transport = FakeTransport()
        result = _checker(transport).check(_lead("https://facebook.com/shop", source="osm"))
        assert result.social_only is True
        assert result.quality == WebsiteQuality.SOCIAL_ONLY
        assert transport.requests == []


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class TestIdentityVerification:
    def test_provided_url_is_reported_as_provided(self):
        transport = FakeTransport().add("owned.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://owned.example"))
        assert result.identity in (WebsiteIdentity.PROVIDED, WebsiteIdentity.TITLE_MATCH)

    def test_title_matching_the_business_is_noted(self):
        transport = FakeTransport().add(
            "titled.example",
            status=200,
            body="<html><head><title>Aden Traders</title></head><body>"
            + "content " * 200
            + "</body></html>",
        )
        result = _checker(transport).check(_lead("https://titled.example"))
        assert result.page_title == "Aden Traders"
        assert result.identity == WebsiteIdentity.TITLE_MATCH

    def test_unrelated_title_does_not_claim_ownership(self):
        transport = FakeTransport().add(
            "unrelated.example",
            status=200,
            body="<html><head><title>Some Other Business Entirely</title></head><body>"
            + "content " * 200
            + "</body></html>",
        )
        result = _checker(transport).check(_lead("https://unrelated.example"))
        # The URL came from the record, so it is still "provided", but ownership
        # is never upgraded on a weak title match.
        assert result.identity != WebsiteIdentity.TITLE_MATCH

    def test_guessed_domain_identity_is_uncertain(self):
        transport = FakeTransport().add("adentraders.com", status=200, body=GOOD_HTML)
        checker = _checker(transport, probe_by_name=True)
        result = checker.check(_lead(None))
        assert result.identity == WebsiteIdentity.UNCERTAIN

    def test_guessed_domain_404_is_unknown_not_not_found(self):
        transport = FakeTransport().add("adentraders.com", status=404)
        checker = _checker(transport, probe_by_name=True)
        result = checker.check(_lead(None))
        assert result.status == WebsiteStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


class TestResultModel:
    def test_carries_verification_detail(self):
        transport = FakeTransport().add("detail.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://detail.example", source="osm"))
        assert result.http_status == 200
        assert result.response_time_ms is not None
        assert result.response_time_ms >= 0
        assert result.checked_at == CHECKED_AT
        assert result.final_url
        assert result.page_title is not None
        assert result.website_source == "osm"

    def test_round_trips_through_dict(self):
        transport = FakeTransport().add("round.example", status=200, body=GOOD_HTML)
        result = _checker(transport).check(_lead("https://round.example"))
        restored = WebsiteCheckResult.from_dict(result.to_dict())
        assert restored.status == result.status
        assert restored.identity == result.identity
        assert restored.page_title == result.page_title

    def test_legacy_payload_without_new_fields_still_loads(self):
        restored = WebsiteCheckResult.from_dict(
            {"status": "website_exists", "website_url": "https://x.example"}
        )
        assert restored.status == WebsiteStatus.EXISTS
        assert restored.error_kind is None
        assert restored.identity == WebsiteIdentity.NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


class TestCaching:
    def test_same_url_is_requested_once_per_run(self):
        transport = FakeTransport().add("same.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        first = checker.check(_lead("https://same.example"))
        second = checker.check(_lead("https://same.example"))
        assert first.status == second.status
        # One request for the scheme probe, reused by the second lead.
        assert len([r for r in transport.requests if "same.example" in r["url"]]) == 1
        assert second.from_cache is True

    def test_cached_result_is_independent_of_the_original(self):
        transport = FakeTransport().add("mut.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        first = checker.check(_lead("https://mut.example"))
        first.notes.append("tampered")
        second = checker.check(_lead("https://mut.example"))
        assert "tampered" not in second.notes

    def test_cache_can_be_disabled(self):
        transport = FakeTransport().add("nocache.example", status=200, body=GOOD_HTML)
        checker = _checker(transport, cache=False)
        checker.check(_lead("https://nocache.example"))
        checker.check(_lead("https://nocache.example"))
        assert len([r for r in transport.requests if "nocache.example" in r["url"]]) == 2

    def test_clear_cache_forgets_previous_checks(self):
        transport = FakeTransport().add("clear.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        checker.check(_lead("https://clear.example"))
        checker.clear_cache()
        checker.check(_lead("https://clear.example"))
        assert len([r for r in transport.requests if "clear.example" in r["url"]]) == 2

    def test_distinct_urls_are_checked_separately(self):
        transport = FakeTransport()
        transport.add("one.example", status=200, body=GOOD_HTML)
        transport.add("two.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        checker.check(_lead("https://one.example"))
        checker.check(_lead("https://two.example"))
        urls = {r["url"] for r in transport.requests}
        assert any("one.example" in u for u in urls)
        assert any("two.example" in u for u in urls)


# --------------------------------------------------------------------------- #
# Determinism and multiple businesses
# --------------------------------------------------------------------------- #


class TestDeterminismAndBatch:
    def test_repeated_checks_are_deterministic(self):
        def run():
            transport = FakeTransport().add("det.example", status=200, body=GOOD_HTML)
            return _checker(transport).check(_lead("https://det.example"))

        first, second = run(), run()
        assert first.to_dict() == second.to_dict()

    def test_multiple_businesses_each_get_a_result(self):
        transport = FakeTransport()
        transport.add("has.example", status=200, body=GOOD_HTML)
        transport.add("gone.example", status=404)
        transport.add_handler(
            "dead.example",
            lambda m, u, p, d, h, t: HttpResponse(0, "", {}, u, error="connection refused"),
        )
        checker = _checker(transport)
        results = checker.check_many(
            [
                _lead("https://has.example"),
                _lead("https://gone.example"),
                _lead("https://dead.example"),
                _lead(None),
            ]
        )
        assert [r.status for r in results] == [
            WebsiteStatus.EXISTS,
            WebsiteStatus.NOT_FOUND,
            WebsiteStatus.UNREACHABLE,
            WebsiteStatus.UNKNOWN,
        ]

    def test_duplicate_website_urls_across_businesses_are_cached_not_redone(self):
        transport = FakeTransport().add("dup.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        checker.check_many([_lead("https://dup.example") for _ in range(5)])
        assert len([r for r in transport.requests if "dup.example" in r["url"]]) == 1


# --------------------------------------------------------------------------- #
# Offline guarantee
# --------------------------------------------------------------------------- #


class TestNoNetworkAccess:
    def test_checker_uses_the_injected_transport_only(self):
        transport = FakeTransport().add("offline.example", status=200, body=GOOD_HTML)
        checker = _checker(transport)
        checker.check(_lead("https://offline.example"))
        assert transport.requests, "the checker must go through the injected transport"

    def test_no_route_matched_is_reported_as_unreachable(self):
        transport = FakeTransport()  # routes nothing
        result = _checker(transport).check(_lead("https://nowhere.example"))
        assert result.status == WebsiteStatus.UNREACHABLE