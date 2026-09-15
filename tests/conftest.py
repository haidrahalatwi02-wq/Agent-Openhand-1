"""Shared pytest fixtures.

Every fixture is offline by design: providers are faked or use the bundled
sample data, and the HTTP layer uses an in-memory transport. The test-suite
must pass with no network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from lead_finder_agent.config.settings import Settings, reset_settings
from lead_finder_agent.models import (
    BusinessStatus,
    Lead,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.utils.http import HttpClient, HttpResponse


# --------------------------------------------------------------------------- #
# Generic fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep tests independent of the developer's real environment and data."""
    for key in (
        "LEAD_FINDER_DB_PATH",
        "LEAD_FINDER_PROVIDERS",
        "LEAD_FINDER_CITY",
        "LEAD_FINDER_COUNTRY",
        "LEAD_FINDER_SCORING_RULES",
        "LEAD_FINDER_DEFAULT_CITY",
        "LEAD_FINDER_DEFAULT_COUNTRY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LEAD_FINDER_DB_PATH", str(tmp_path / "test.db"))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "leads.db",
        default_country="Yemen",
        default_city="Aden",
    )


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Lead factories
# --------------------------------------------------------------------------- #


@pytest.fixture
def make_lead() -> Callable[..., Lead]:
    """Factory producing leads with sensible defaults per test case."""

    def _make(**overrides: Any) -> Lead:
        data: Dict[str, Any] = {
            "business_name": "Test Business",
            "city": "Aden",
            "country": "Yemen",
            "business_type": "restaurant",
        }
        data.update(overrides)
        return Lead(**data)

    return _make


@pytest.fixture
def lead_without_website(make_lead: Callable[..., Lead]) -> Lead:
    return make_lead(
        business_name="Aden Traders",
        phone="+967 71 000 1111",
        address="Main Street",
        business_status=BusinessStatus.ACTIVE,
        website_status=WebsiteStatus.NOT_FOUND,
        categories=["shop"],
    )


@pytest.fixture
def lead_with_good_website(make_lead: Callable[..., Lead]) -> Lead:
    return make_lead(
        business_name="Modern Electronics",
        website_url="https://modern.example",
        website_status=WebsiteStatus.EXISTS,
        website_quality=WebsiteQuality.GOOD,
        business_status=BusinessStatus.ACTIVE,
        phone="+967 71 222 3333",
    )


# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #


class FakeTransport:
    """Routes requests to canned responses by URL substring.

    Register a handler with :meth:`add`; a request whose URL matches multiple
    handlers uses the longest matching key, so ``example.com/shop`` can override
    a broader ``example.com`` rule.
    """

    def __init__(self) -> None:
        self.routes: Dict[str, Callable[..., HttpResponse]] = {}
        self.requests: List[Dict[str, Any]] = []

    def add(
        self,
        match: str,
        status: int = 200,
        body: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> "FakeTransport":
        payload = dict(headers or {})

        def handler(method, url, params, data, req_headers, timeout) -> HttpResponse:
            return HttpResponse(status, body, payload, url)

        self.routes[match] = handler
        return self

    def add_handler(self, match: str, handler: Callable[..., HttpResponse]) -> "FakeTransport":
        self.routes[match] = handler
        return self

    def __call__(self, method, url, params, data, headers, timeout) -> HttpResponse:
        self.requests.append(
            {"method": method, "url": url, "params": params, "data": data, "headers": headers}
        )
        matches = [key for key in self.routes if key in url]
        if not matches:
            return HttpResponse(504, "", {}, url, error="no route matched")
        key = max(matches, key=len)
        return self.routes[key](method, url, params, data, headers, timeout)

    def client(self, **kwargs: Any) -> HttpClient:
        return HttpClient(transport=self, **kwargs)


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def offline_client(fake_transport: FakeTransport) -> HttpClient:
    """A client whose requests always fail, simulating no connectivity."""

    def _fail(method, url, params, data, headers, timeout) -> HttpResponse:
        return HttpResponse(0, "", {}, url, error="network unavailable")

    return HttpClient(transport=_fail)


# --------------------------------------------------------------------------- #
# HTML pages used by checker tests
# --------------------------------------------------------------------------- #


@pytest.fixture
def good_page_html() -> str:
    return (
        "<html><head><title>Aden Traders - Home</title></head><body>"
        + ("Welcome to Aden Traders. We sell quality goods. " * 20)
        + '<a href="/contact">Contact us</a><a href="/cart">Add to cart</a>'
        + '<a href="/checkout">Checkout</a><footer>&copy; 2026 Aden Traders</footer>'
        "</body></html>"
    )


@pytest.fixture
def thin_page_html() -> str:
    return "<html><head><title>hi</title></head><body>Coming soon</body></html>"


@pytest.fixture
def placeholder_html() -> str:
    return (
        "<html><body>This domain is for sale. Domain for sale - buy this domain.</body></html>"
    )


@pytest.fixture
def outdated_html() -> str:
    return (
        "<html><body>"
        + ("Some old content that is long enough to not be considered thin. " * 10)
        + "<footer>&copy; 2015 Old Site</footer></body></html>"
    )


@pytest.fixture
def osm_overpass_payload() -> Dict[str, Any]:
    return {
        "elements": [
            {
                "type": "node",
                "id": 101,
                "lat": 12.7855,
                "lon": 45.0187,
                "tags": {
                    "name": "Al Bahr Seafood Restaurant",
                    "amenity": "restaurant",
                    "phone": "+967 71 234 5678",
                    "addr:street": "Corniche Road",
                    "addr:city": "Aden",
                    "website": "https://facebook.com/albahrseafood",
                },
            },
            {
                "type": "node",
                "id": 102,
                "lat": 12.7900,
                "lon": 45.0200,
                "tags": {
                    "name": "Golden Star Bakery",
                    "shop": "bakery",
                    "phone": "+967 72 333 8899",
                    "addr:street": "Sheikh Othman",
                },
            },
            {
                "type": "node",
                "id": 103,
                "lat": 12.7910,
                "lon": 45.0210,
                # No name: must be dropped by the provider.
                "tags": {"shop": "bakery"},
            },
        ]
    }


@pytest.fixture
def nominatim_payload() -> List[Dict[str, Any]]:
    return [
        {
            "display_name": "Aden, Yemen",
            "lat": "12.7855",
            "lon": "45.0187",
            "boundingbox": ["12.70", "12.90", "44.90", "45.10"],
        }
    ]


@pytest.fixture
def sample_provider_config() -> Dict[str, Any]:
    return {"default_city": "Aden", "default_country": "Yemen"}


def json_response(payload: Any, status: int = 200) -> HttpResponse:
    """Helper for building a JSON HTTP response in tests."""
    return HttpResponse(status, json.dumps(payload), {"content-type": "application/json"})


__all__ = ["FakeTransport", "json_response"]
