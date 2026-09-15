"""Tests for the shared utilities (HTTP, text, logging)."""

from __future__ import annotations

import json
import logging

import pytest

from lead_finder_agent.utils.http import HttpClient, HttpResponse
from lead_finder_agent.utils.logging_utils import get_logger, setup_logging
from lead_finder_agent.utils.text import (
    extract_domain,
    normalize_whitespace,
    parse_keywords,
    registrable_domain,
    safe_int,
    slugify,
    truncate,
)

from tests.conftest import FakeTransport


class TestTextHelpers:
    def test_normalize_whitespace(self):
        assert normalize_whitespace("  a   b  ") == "a b"
        assert normalize_whitespace("") is None
        assert normalize_whitespace(None) is None

    def test_truncate_adds_ellipsis(self):
        assert truncate("abcdef", 4) == "abc\u2026"
        assert truncate("abc", 10) == "abc"

    def test_slugify_is_ascii_and_safe(self):
        assert slugify("Al Bahr Restaurant!") == "al-bahr-restaurant"

    def test_parse_keywords_accepts_many_types(self):
        assert parse_keywords("a, b ,c") == ["a", "b", "c"]
        assert parse_keywords(["a", "b"]) == ["a", "b"]
        assert parse_keywords(None) == []
        assert parse_keywords("  ") == []

    def test_safe_int(self):
        assert safe_int("42") == 42
        assert safe_int("4.7") == 4
        assert safe_int("1,000") == 1000
        assert safe_int("abc", default=7) == 7
        assert safe_int(None) is None

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.example.com/path", "example.com"),
            ("http://sub.example.com", "sub.example.com"),
            ("example.com", "example.com"),
            (None, None),
        ],
    )
    def test_extract_domain(self, url, expected):
        assert extract_domain(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://shop.example.co.uk/a", "example.co.uk"),
            ("https://www.example.com", "example.com"),
            ("https://a.b.c.example.org", "example.org"),
            ("https://192.168.1.1/x", "192.168.1.1"),
        ],
    )
    def test_registrable_domain(self, url, expected):
        assert registrable_domain(url) == expected


class TestHttpClient:
    def test_get_uses_transport(self, fake_transport: FakeTransport):
        fake_transport.add("example.com", status=200, body="ok")
        response = fake_transport.client().get("https://example.com")
        assert response.status_code == 200
        assert response.ok
        assert response.text == "ok"

    def test_sends_user_agent_header(self, fake_transport: FakeTransport):
        fake_transport.add("example.com")
        fake_transport.client(user_agent="TestAgent/1.0").get("https://example.com")
        assert fake_transport.requests[0]["headers"]["User-Agent"] == "TestAgent/1.0"

    def test_transport_error_is_returned_not_raised(self):
        def boom(method, url, params, data, headers, timeout):
            return HttpResponse(0, "", {}, url, error="dns failure")

        response = HttpClient(transport=boom).get("https://nope.example")
        assert response.ok is False
        assert response.error == "dns failure"

    def test_json_helper(self, fake_transport: FakeTransport):
        fake_transport.add("api.example", status=200, body=json.dumps({"a": 1}))
        assert fake_transport.client().get("https://api.example").json() == {"a": 1}

    def test_post_passes_data(self, fake_transport: FakeTransport):
        fake_transport.add("api.example", status=200, body="{}")
        fake_transport.client().post("https://api.example", data={"q": "x"})
        assert fake_transport.requests[0]["data"] == {"q": "x"}

    def test_retries_transient_failures(self):
        calls = {"n": 0}

        def flaky(method, url, params, data, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return HttpResponse(0, "", {}, url, error="timeout")
            return HttpResponse(200, "recovered", {}, url)

        response = HttpClient(transport=flaky, max_retries=1).get("https://x.example")
        assert response.status_code == 200
        assert calls["n"] == 2

    def test_no_retry_beyond_max(self):
        calls = {"n": 0}

        def always_fails(method, url, params, data, headers, timeout):
            calls["n"] += 1
            return HttpResponse(0, "", {}, url, error="down")

        response = HttpClient(transport=always_fails, max_retries=2).get("https://x.example")
        assert response.error == "down"
        assert calls["n"] == 3

    def test_http_response_ok_range(self):
        assert HttpResponse(200).ok is True
        assert HttpResponse(301).ok is True
        assert HttpResponse(404).ok is False
        assert HttpResponse(0, error="x").ok is False


class TestLogging:
    def test_get_logger_namespaces(self):
        assert get_logger("mymodule").name == "lead_finder_agent.mymodule"

    def test_get_logger_keeps_existing_namespace(self):
        assert get_logger("lead_finder_agent.x").name == "lead_finder_agent.x"

    def test_setup_logging_sets_level(self):
        setup_logging("DEBUG", force=True)
        assert logging.getLogger("lead_finder_agent").level == logging.DEBUG
        setup_logging("INFO", force=True)
        assert logging.getLogger("lead_finder_agent").level == logging.INFO

    def test_invalid_level_falls_back_to_info(self):
        setup_logging("NOTALEVEL", force=True)
        assert logging.getLogger("lead_finder_agent").level == logging.INFO