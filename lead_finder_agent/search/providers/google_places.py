"""Google Places API (New) provider.

Discovers real businesses through Google's official, documented
`Places API (New) <https://developers.google.com/maps/documentation/places/web-service/text-search>`_
``places:searchText`` endpoint. It is a keyed, paid, terms-of-service-bound API,
so unlike the OpenStreetMap provider it needs a credential and is not free.

Design notes
------------
*Nothing about a country is hard-coded.* The search text is assembled from
whatever the caller supplied --- ``business_type``, ``keywords``, ``city`` and
``country`` --- so the provider works wherever Google has coverage.

*Only public business fields are requested.* The field mask asks for the
business record and nothing else; no personal data is collected, and the
provider never contacts a business.

*The credential is never logged or returned.* The key is read from configuration
or an environment variable and placed only into the ``X-Goog-Api-Key`` header.
Error messages describe the status code, never the key.

Pagination
----------
Google caps ``pageSize`` at 20 and returns a ``nextPageToken``. The provider
follows that token until it has enough results, honouring the caller's limit
exactly and stopping at ``max_pages`` as a safety net.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from lead_finder_agent.models import ProviderKind, SearchQuery
from lead_finder_agent.search.base import (
    BaseSearchProvider,
    ProviderError,
    ProviderErrorKind,
)
from lead_finder_agent.search.registry import register_provider
from lead_finder_agent.utils.http import HttpClient, HttpResponse
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.providers.google_places")

DEFAULT_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

#: The environment variable holding the credential. Referenced in one place so
#: it can be surfaced by `lead-finder providers` and documented consistently.
DEFAULT_API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

#: Google rejects a larger page size; 20 is the documented maximum.
MAX_PAGE_SIZE = 20

#: Default safety cap on pagination. Google's text search tops out around 60
#: results, and an unbounded loop against a misbehaving endpoint would be worse.
DEFAULT_MAX_PAGES = 3

#: Exactly the fields this provider consumes. A tight mask keeps the response
#: small, avoids paying for fields nobody reads, and makes it obvious that no
#: personal data is requested.
DEFAULT_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.shortFormattedAddress",
        "places.addressComponents",
        "places.types",
        "places.primaryType",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)

#: ``longText`` of an address component, by the component types we want.
_CITY_COMPONENT_TYPES = (
    "locality",
    "postal_town",
    "administrative_area_level_2",
    "administrative_area_level_1",
)
_COUNTRY_COMPONENT_TYPES = ("country",)

#: Google's lifecycle values mapped onto our own vocabulary. A temporarily
#: closed shop is deliberately *not* reported as closed: it has not gone away.
_BUSINESS_STATUS = {
    "OPERATIONAL": "active",
    "CLOSED_PERMANENTLY": "closed",
    "CLOSED_TEMPORARILY": "unknown",
    "FUTURE_OPENING": "unknown",
}

#: Statuses meaning "this request will never succeed as written".
_AUTH_STATUSES = (400, 401, 403)
_RATE_LIMIT_STATUS = 429


@register_provider
class GooglePlacesProvider(BaseSearchProvider):
    """Real business discovery via the Google Places API (New)."""

    name = "google_places"
    kind = ProviderKind.API
    requires_key_env = DEFAULT_API_KEY_ENV
    description = "Google Places API (New) - real businesses, requires an API key"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = self.config
        self.endpoint = str(cfg.get("google_places_endpoint") or DEFAULT_ENDPOINT)
        self.key_env = str(cfg.get("api_key_env") or DEFAULT_API_KEY_ENV)
        # Read once at construction and never written to a log or an exception.
        self._api_key = str(cfg.get("api_key") or os.getenv(self.key_env) or "").strip()
        self.field_mask = str(cfg.get("field_mask") or DEFAULT_FIELD_MASK)
        self.language = cfg.get("language_code")
        self.page_size = self._clamp_page_size(cfg.get("page_size") or MAX_PAGE_SIZE)
        self.max_pages = max(1, int(cfg.get("max_pages") or DEFAULT_MAX_PAGES))
        self.client: HttpClient = cfg.get("client") or HttpClient(
            user_agent=cfg.get("user_agent") or "LeadFinderAgent/0.1",
            timeout=float(cfg.get("http_timeout") or 12.0),
            # Retrying a 429 or a 5xx in-process would only deepen a rate limit,
            # so a failed page is reported and left to the caller to re-run.
            max_retries=0,
        )

    # -- availability ------------------------------------------------------

    def is_available(self) -> bool:
        """Whether a credential is present (from config or the environment)."""
        return bool(self._api_key)

    def unavailable_reason(self) -> Optional[str]:
        if self.is_available():
            return None
        return (
            f"{self.name} requires an API key: set {self.key_env} "
            "or pass 'api_key' in the provider config"
        )

    # -- search ------------------------------------------------------------

    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """Return raw business records for ``query``, following pagination."""
        if not self._api_key:
            # Defensive: ``search`` normally short-circuits on is_available().
            raise ProviderError(
                f"No API key available; set {self.key_env}",
                kind=ProviderErrorKind.MISSING_KEY,
            )

        wanted = int(query.limit)
        if wanted <= 0:
            return []

        text_query = self._build_text_query(query)
        collected: List[Dict[str, Any]] = []
        seen_ids: set = set()
        page_token: Optional[str] = None
        page_size = min(self.page_size, wanted)

        for _ in range(self.max_pages):
            payload = self._fetch_page(text_query, page_size, page_token)

            places = payload.get("places")
            if places is None:
                places = []
            if not isinstance(places, list):
                raise ProviderError(
                    "'places' was not a list; the response shape was not understood",
                    kind=ProviderErrorKind.MALFORMED_RESPONSE,
                )

            for place in places:
                if not isinstance(place, dict):
                    # One bad entry must not discard the rest of the page.
                    log.debug("Skipping non-object place entry: %s", type(place).__name__)
                    continue
                record = self._to_raw(place, query)
                if record is None:
                    continue
                # De-duplicate on Google's stable place id when it is present;
                # otherwise fall back to a deterministic key built from public
                # fields, so a repeated record does not become a repeated lead.
                dedupe_key = record.get("source_id") or self._fallback_key(record)
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                collected.append(record)
                if len(collected) >= wanted:
                    break

            if len(collected) >= wanted:
                break

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            page_size = min(MAX_PAGE_SIZE, wanted - len(collected))

        # Never hand back more than was asked for, whatever the API returned.
        return collected[:wanted]

    # -- request/response --------------------------------------------------

    def _fetch_page(
        self, text_query: str, page_size: int, page_token: Optional[str]
    ) -> Dict[str, Any]:
        """Fetch and validate a single page of results."""
        body: Dict[str, Any] = {"textQuery": text_query, "pageSize": page_size}
        if page_token:
            body["pageToken"] = page_token
        if self.language:
            body["languageCode"] = str(self.language)

        response = self.client.post(
            self.endpoint,
            # The v1 API takes a JSON body. HttpClient passes ``data`` straight
            # to the transport, so a serialized string plus the right
            # Content-Type reaches both `requests` and the offline test
            # transport without a second code path.
            data=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                # The key goes here and nowhere else. It is never logged.
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": self.field_mask,
            },
        )
        self._note_page()
        self._raise_for_status(response)

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any parse failure is unusable
            raise ProviderError(
                f"Could not parse the JSON response: {exc}",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            ) from exc

        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ProviderError(
                "Expected a JSON object at the top level of the response",
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
            )
        return payload

    @staticmethod
    def _raise_for_status(response: HttpResponse) -> None:
        """Translate a failed HTTP response into a classified error."""
        if response.error is not None:
            raise ProviderError(
                f"Request failed: {response.error}",
                kind=ProviderErrorKind.NETWORK,
            )

        status = int(response.status_code or 0)
        if 200 <= status < 300:
            return

        if status == _RATE_LIMIT_STATUS:
            raise ProviderError(
                "The provider rate limit or quota was exceeded (HTTP 429); "
                "retry later or reduce --limit",
                kind=ProviderErrorKind.RATE_LIMITED,
            )
        if status in _AUTH_STATUSES:
            # Say what is wrong without ever echoing the credential itself.
            raise ProviderError(
                f"The API rejected the request (HTTP {status}). Check that the "
                "API key is valid, that the Places API (New) is enabled, and "
                "that the key allows this call",
                kind=ProviderErrorKind.INVALID_CONFIG,
            )
        if status >= 500:
            raise ProviderError(
                f"The provider reported a server error (HTTP {status})",
                kind=ProviderErrorKind.PROVIDER_ERROR,
            )
        raise ProviderError(
            f"Unexpected response from the provider (HTTP {status})",
            kind=ProviderErrorKind.PROVIDER_ERROR,
        )

    # -- query building ----------------------------------------------------

    def _build_text_query(self, query: SearchQuery) -> str:
        """Compose the free-text query from the caller's request.

        No country, city or vertical is assumed: the caller's values are used
        verbatim, and the location clause is omitted when none was given.
        """
        subject_parts: List[str] = []
        if query.business_type:
            subject_parts.append(query.business_type)
        subject_parts.extend(query.keywords or [])
        subject = " ".join(part for part in subject_parts if part).strip()

        location = ", ".join(part for part in (query.city, query.country) if part)

        if subject and location:
            return f"{subject} in {location}"
        if subject:
            return subject
        if location:
            # Without a vertical, "businesses in <place>" is the honest query.
            return f"businesses in {location}"
        return "businesses"

    # -- record mapping ----------------------------------------------------

    def _to_raw(
        self, place: Dict[str, Any], query: SearchQuery
    ) -> Optional[Dict[str, Any]]:
        """Map one Google place onto a raw record for the normalizer."""
        place_id = str(place.get("id") or "").strip()
        name = self._display_name(place)
        if not name:
            return None  # an unnamed record is not an actionable lead

        components = place.get("addressComponents")
        components = components if isinstance(components, list) else []
        city = self._component(components, _CITY_COMPONENT_TYPES)
        country = self._component(components, _COUNTRY_COMPONENT_TYPES)

        location = place.get("location")
        location = location if isinstance(location, dict) else {}

        record: Dict[str, Any] = {
            "source_id": place_id or None,
            "business_name": name,
            "business_type": (
                self._to_text(place.get("primaryType"))
                or self._first_type(place.get("types"))
                or query.business_type
            ),
            "categories": self._clean_types(place.get("types")),
            "address": self._to_text(place.get("formattedAddress"))
            or self._to_text(place.get("shortFormattedAddress")),
            # Prefer what the source states; otherwise fall back to the search
            # area, which is context the caller supplied, not an invented value.
            "city": city or query.city,
            "country": country or query.country,
            "phone": self._to_text(place.get("nationalPhoneNumber"))
            or self._to_text(place.get("internationalPhoneNumber")),
            "website_url": self._to_text(place.get("websiteUri")),
            "latitude": self._to_float(location.get("latitude")),
            "longitude": self._to_float(location.get("longitude")),
            "rating": self._to_float(place.get("rating")),
            "review_count": self._to_int(place.get("userRatingCount")),
            "business_status": _BUSINESS_STATUS.get(
                str(place.get("businessStatus") or "").upper()
            ),
            "source_url": self._to_text(place.get("googleMapsUri")),
        }
        # Drop empty values so we never assert something the source did not
        # provide; the normalizer treats an absent key as "unknown".
        return {key: value for key, value in record.items() if value not in (None, "", [])}

    @staticmethod
    def _fallback_key(record: Dict[str, Any]) -> str:
        """Deterministic identity for a record the source did not give an id.

        Built only from public fields that identify a business in practice.
        Two genuinely different shops with the same name in the same city would
        collide, which is the accepted trade-off: a stable key is required for
        deduplication, and Google does supply ``id`` in every real response.
        """
        parts = [
            str(record.get("business_name") or ""),
            str(record.get("address") or ""),
            str(record.get("city") or ""),
            str(record.get("country") or ""),
        ]
        normalized = "|".join(" ".join(part.lower().split()) for part in parts)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"fallback:{digest}"

    @staticmethod
    def _display_name(place: Dict[str, Any]) -> str:
        """Extract the display name, which the v1 API nests as ``{"text": ...}``."""
        display = place.get("displayName")
        if isinstance(display, dict):
            return str(display.get("text") or "").strip()
        return str(display or "").strip()

    @staticmethod
    def _component(components: List[Any], wanted_types: tuple) -> Optional[str]:
        """Pull a city or country value out of ``addressComponents``."""
        for component in components:
            if not isinstance(component, dict):
                continue
            types = component.get("types")
            types = types if isinstance(types, list) else []
            if any(str(item) in wanted_types for item in types):
                text = component.get("longText") or component.get("shortText")
                if text:
                    return str(text).strip()
        return None

    @staticmethod
    def _clean_types(types: Any) -> List[str]:
        if not isinstance(types, list):
            return []
        return [str(item) for item in types if isinstance(item, (str, int))]

    @staticmethod
    def _first_type(types: Any) -> Optional[str]:
        if isinstance(types, list):
            for item in types:
                if isinstance(item, str) and item:
                    return item
        return None

    @staticmethod
    def _to_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clamp_page_size(value: Any) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError):
            return MAX_PAGE_SIZE
        return max(1, min(MAX_PAGE_SIZE, size))

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


__all__ = [
    "GooglePlacesProvider",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_ENDPOINT",
    "MAX_PAGE_SIZE",
]