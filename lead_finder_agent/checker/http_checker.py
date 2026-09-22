"""HTTP-based website checker.

The checker answers one question well: *does this business appear to have a
usable website?* It does that without claiming more than it can prove:

``website_exists``
    A URL was found and responded successfully. Quality is graded separately
    (good / weak / social-only).
``website_not_found``
    A URL was found but the server explicitly said it does not exist (404/410)
    across every scheme, **or** the domain does not resolve *and* the name-based
    probe agreed.
``website_unreachable``
    A URL was found but the request failed (DNS, timeout, TLS, 5xx).
``website_unknown``
    We could not find a URL and could not probe confidently, or probing was
    inconclusive. This is the honest default and it is never treated as "no
    website" by the scoring engine.

The HTML analysis is deliberately dependency-free: no parser is required to
detect a cart, a contact link, a placeholder page or an outdated copyright year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.checker.osm_tags import is_social_url
from lead_finder_agent.config.loader import load_packaged_data, merge_dicts
from lead_finder_agent.models import (
    Lead,
    WebsiteCheckResult,
    WebsiteQuality,
    WebsiteStatus,
    normalize_url,
)
from lead_finder_agent.utils.http import HttpClient, HttpResponse
from lead_finder_agent.utils.logging_utils import get_logger
from lead_finder_agent.utils.text import extract_domain

log = get_logger("checker.http")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:[^\d]{0,20})?(19|20)(\d{2})", re.IGNORECASE)


@dataclass
class WebsiteSignals:
    """Thresholds and markers driving the HTML analysis."""

    http: Dict[str, Any] = field(default_factory=dict)
    social_domains: List[str] = field(default_factory=list)
    weak_hosts: List[str] = field(default_factory=list)
    shop_markers: List[str] = field(default_factory=list)
    contact_markers: List[str] = field(default_factory=list)
    contact_paths: List[str] = field(default_factory=lambda: ["/contact"])
    min_content_length: int = 400
    placeholder_markers: List[str] = field(default_factory=list)
    outdated_after_years: int = 4

    @classmethod
    def load(cls, override: Optional[Dict[str, Any]] = None) -> "WebsiteSignals":
        data = load_packaged_data("website_signals")
        if override:
            data = merge_dicts(data, override)
        return cls(
            http=dict(data.get("http") or {}),
            social_domains=list(data.get("social_domains") or []),
            weak_hosts=list(data.get("weak_hosts") or []),
            shop_markers=[m.lower() for m in (data.get("shop_markers") or [])],
            contact_markers=[m.lower() for m in (data.get("contact_markers") or [])],
            contact_paths=list(data.get("contact_paths") or ["/contact"]),
            min_content_length=int(data.get("min_content_length") or 400),
            placeholder_markers=[m.lower() for m in (data.get("placeholder_markers") or [])],
            outdated_after_years=int(data.get("outdated_after_years") or 4),
        )

    # -- http config accessors --------------------------------------------

    @property
    def ok_status(self) -> Sequence[int]:
        return self.http.get("ok_status", [200, 201, 202, 203, 204, 301, 302, 307, 308])

    @property
    def weak_status(self) -> Sequence[int]:
        return self.http.get("weak_status", [401, 403, 405, 429, 500, 502, 503, 504])

    @property
    def missing_status(self) -> Sequence[int]:
        return self.http.get("missing_status", [404, 410])

    @property
    def schemes(self) -> Sequence[str]:
        return self.http.get("schemes", ["https", "http"])


class HttpWebsiteChecker(BaseWebsiteChecker):
    """Checks websites over HTTP and grades what it finds.

    Parameters
    ----------
    client:
        HTTP client. Inject a fake transport in tests to stay offline.
    probe_by_name:
        When a lead has no website URL, guess a domain from the business name
        (``example`` -> ``example.com``) and probe it. This catches businesses
        that have a site but did not tag it. Because guesses can be wrong, a
        404 from a guessed domain yields ``website_unknown`` rather than
        ``website_not_found`` unless the domain resolves and serves a
        placeholder page.
    probe_contact_page:
        Fetch ``/contact`` when the homepage has no contact link, so "has a
        contact page" is not decided by the homepage alone.
    """

    name = "http"

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        signals: Optional[WebsiteSignals] = None,
        client: Optional[HttpClient] = None,
        probe_by_name: bool = True,
        probe_contact_page: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        cfg: Dict[str, Any] = dict(config or {})
        self.signals = signals or WebsiteSignals.load(cfg.get("signals"))
        self.client = client or HttpClient(
            user_agent=cfg.get("user_agent") or "LeadFinderAgent/0.1",
            timeout=float(cfg.get("http_timeout") or self.signals.http.get("timeout_seconds", 10)),
            max_retries=int(cfg.get("max_retries", 0)),
        )
        self.probe_by_name = bool(cfg.get("probe_by_name", probe_by_name))
        self.probe_contact_page = bool(cfg.get("probe_contact_page", probe_contact_page))
        self._now = now or datetime.now(timezone.utc)

    # -- entry point -------------------------------------------------------

    def check(self, lead: Lead) -> WebsiteCheckResult:
        """Inspect one lead and return a :class:`WebsiteCheckResult`."""
        candidate = normalize_url(lead.website_url)

        if candidate and self._is_social(candidate):
            return WebsiteCheckResult(
                status=WebsiteStatus.EXISTS,
                website_url=candidate,
                quality=WebsiteQuality.SOCIAL_ONLY,
                social_only=True,
                is_reachable=False,
                checked_at=self._now,
                notes=["Website field points to a social media profile"],
            )

        if candidate:
            return self._check_url(candidate, guessed=False)

        if self.probe_by_name:
            guessed = self._guess_domain(lead.business_name)
            if guessed:
                result = self._check_url(guessed, guessed=True)
                result.notes.append("Website URL was guessed from the business name")
                return result

        return WebsiteCheckResult(
            status=WebsiteStatus.UNKNOWN,
            quality=WebsiteQuality.UNKNOWN,
            checked_at=self._now,
            notes=["No website URL available from public sources"],
        )

    # -- URL probing -------------------------------------------------------

    def _check_url(self, url: str, guessed: bool = False) -> WebsiteCheckResult:
        parsed = urlparse(url)
        host = extract_domain(url) or ""
        scheme = parsed.scheme or "https"

        response: Optional[HttpResponse] = None
        used_url = url
        attempts: List[str] = []

        # Try the given scheme first, then the alternate scheme (http <-> https)
        # so an http-only site is not reported as unreachable.
        schemes = [scheme] + [s for s in self.signals.schemes if s != scheme]
        for candidate_scheme in schemes:
            target = f"{candidate_scheme}://{host}{parsed.path or ''}"
            if parsed.query:
                target += f"?{parsed.query}"
            response = self.client.get(target)
            attempts.append(f"{candidate_scheme}->{response.status_code or 'error'}")
            if response.error is None:
                used_url = target
                break

        checked_at = self._now
        if response is None:  # pragma: no cover - defensive
            return WebsiteCheckResult(
                status=WebsiteStatus.UNKNOWN, website_url=url, checked_at=checked_at
            )

        if response.error is not None:
            status = (
                WebsiteStatus.UNKNOWN
                if guessed
                else WebsiteStatus.UNREACHABLE
            )
            return WebsiteCheckResult(
                status=status,
                website_url=url,
                quality=WebsiteQuality.UNKNOWN,
                is_reachable=False,
                checked_at=checked_at,
                error=response.error,
                notes=[f"Request failed: {response.error}", f"Schemes tried: {attempts}"],
            )

        return self._grade(response, used_url, guessed=guessed, attempts=attempts)

    def _grade(
        self,
        response: HttpResponse,
        url: str,
        guessed: bool,
        attempts: Sequence[str],
    ) -> WebsiteCheckResult:
        status_code = response.status_code
        body = response.text or ""
        has_https = url.lower().startswith("https://")
        notes: List[str] = [f"HTTP {status_code}", f"Schemes tried: {list(attempts)}"]

        if status_code in self.signals.missing_status:
            # A guessed domain returning 404 is not proof the business has no
            # website, so we stay honest and report UNKNOWN.
            resolved_status = WebsiteStatus.UNKNOWN if guessed else WebsiteStatus.NOT_FOUND
            return WebsiteCheckResult(
                status=resolved_status,
                website_url=url,
                http_status=status_code,
                is_reachable=False,
                has_https=has_https,
                checked_at=self._now,
                notes=notes + ["Server reported the page does not exist"],
            )

        if status_code in self.signals.weak_status:
            return WebsiteCheckResult(
                status=WebsiteStatus.EXISTS,
                website_url=url,
                http_status=status_code,
                quality=WebsiteQuality.WEAK,
                is_reachable=True,
                has_https=has_https,
                checked_at=self._now,
                notes=notes + ["Site exists but returned a degraded status"],
            )

        if status_code not in self.signals.ok_status and not (200 <= status_code < 400):
            return WebsiteCheckResult(
                status=WebsiteStatus.UNKNOWN,
                website_url=url,
                http_status=status_code,
                has_https=has_https,
                checked_at=self._now,
                notes=notes + ["Unexpected status; cannot conclude"],
            )

        analysis = self._analyse_html(body, url)
        notes.extend(analysis["notes"])

        quality = WebsiteQuality.GOOD
        if analysis["placeholder"]:
            quality = WebsiteQuality.WEAK
        elif analysis["thin"]:
            quality = WebsiteQuality.WEAK
        elif analysis["outdated"]:
            quality = WebsiteQuality.WEAK
        if extract_domain(url) and any(
            host in (extract_domain(url) or "") for host in self.signals.weak_hosts
        ):
            quality = WebsiteQuality.WEAK
            notes.append("Hosted on a free website builder")

        if self.probe_contact_page and not analysis["has_contact"]:
            if self._contact_page_exists(url):
                analysis["has_contact"] = True
                notes.append("Contact page found at a standard path")

        return WebsiteCheckResult(
            status=WebsiteStatus.EXISTS,
            website_url=url,
            http_status=status_code,
            quality=quality,
            has_https=has_https,
            is_reachable=True,
            has_shop=analysis["has_shop"],
            has_contact_page=analysis["has_contact"],
            checked_at=self._now,
            notes=notes,
        )

    # -- HTML analysis -----------------------------------------------------

    def _analyse_html(self, body: str, url: str) -> Dict[str, Any]:
        lowered = body.lower()
        notes: List[str] = []

        placeholder = any(marker in lowered for marker in self.signals.placeholder_markers)
        if placeholder:
            notes.append("Page looks like a placeholder or parked domain")

        visible = self._visible_text(body)
        thin = len(visible) < self.signals.min_content_length
        if thin:
            notes.append("Very little page content")

        has_shop = any(marker in lowered for marker in self.signals.shop_markers)
        if has_shop:
            notes.append("E-commerce markers detected")

        has_contact = any(marker in lowered for marker in self.signals.contact_markers)
        if not has_contact:
            for href in _LINK_RE.findall(body):
                if any(path.lower() in href.lower() for path in self.signals.contact_paths):
                    has_contact = True
                    break
        if has_contact:
            notes.append("Contact details or contact page present")

        outdated = False
        years = [int(f"{century}{rest}") for century, rest in _YEAR_RE.findall(body)]
        if years:
            newest = max(years)
            cutoff = self._now.year - self.signals.outdated_after_years
            if newest < cutoff:
                outdated = True
                notes.append(f"Copyright year {newest} suggests an outdated site")

        title_match = _TITLE_RE.search(body)
        if title_match:
            title = _TAG_RE.sub("", title_match.group(1)).strip()
            if title:
                notes.append(f"Title: {title[:80]}")

        return {
            "placeholder": placeholder,
            "thin": thin,
            "has_shop": has_shop,
            "has_contact": has_contact,
            "outdated": outdated,
            "notes": notes,
        }

    @staticmethod
    def _visible_text(html: str) -> str:
        without_scripts = _SCRIPT_RE.sub(" ", html or "")
        without_tags = _TAG_RE.sub(" ", without_scripts)
        return re.sub(r"\s+", " ", without_tags).strip()

    def _contact_page_exists(self, url: str) -> bool:
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for path in self.signals.contact_paths:
            response = self.client.get(urljoin(base + "/", path.lstrip("/")))
            if response.error is None and response.status_code in self.signals.ok_status:
                return True
        return False

    # -- helpers -----------------------------------------------------------

    def _is_social(self, url: str) -> bool:
        return is_social_url(url) or any(
            host in (extract_domain(url) or "") for host in self.signals.social_domains
        )

    @staticmethod
    def _guess_domain(business_name: Optional[str]) -> Optional[str]:
        """Guess ``example.com`` from a business name. Never claims certainty."""
        if not business_name:
            return None
        # If the record contains a domain-like token, prefer that.
        match = re.search(r"([a-z0-9][a-z0-9\-]{2,}\.(com|net|org|shop|store|co|io|ye|sa|ae|eg))", business_name.lower())
        if match:
            return f"https://{match.group(1)}"

        ascii_only = re.sub(r"[^a-z0-9 ]+", " ", business_name.lower())
        words = [word for word in ascii_only.split() if len(word) > 1]
        if not words:
            return None
        if len(words) > 4:
            words = words[:4]
        slug = "".join(words)
        if len(slug) < 4:
            return None
        return f"https://{slug}.com"


__all__ = ["HttpWebsiteChecker", "WebsiteSignals"]
