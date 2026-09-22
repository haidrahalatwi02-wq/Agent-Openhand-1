"""HTTP client with a pluggable transport.

Using ``requests`` when it is installed and falling back to the standard library
``urllib`` keeps the package installable in restricted environments. Tests
inject a fake transport so no network access is required.
"""

from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urljoin

from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("utils.http")

DEFAULT_USER_AGENT = (
    "LeadFinderAgent/0.1 (+https://github.com/haidrahalatwi02-wq/Agent-Openhand-1)"
)


class HttpError(Exception):
    """Raised for transport level failures (connection, DNS, timeout)."""


class ResponseTooLarge(HttpError):
    """Raised when a response body exceeds the configured limit."""


class RedirectLimitExceeded(HttpError):
    """Raised when a redirect chain is longer than the configured limit."""


@dataclass
class HttpResponse:
    """Normalized HTTP response independent of the underlying library."""

    status_code: int
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 400

    def json(self) -> Any:
        return _json.loads(self.text or "null")


class HttpClient:
    """Thin HTTP wrapper.

    Parameters
    ----------
    user_agent:
        Sent on every request. A descriptive value is required by OpenStreetMap
        and good practice everywhere else.
    timeout:
        Per-request timeout in seconds.
    transport:
        Optional callable ``(method, url, params, data, headers, timeout) ->
        HttpResponse``. Supplying one bypasses the network entirely, which is
        what the test-suite does.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 12.0,
        transport: Optional[Callable[..., HttpResponse]] = None,
        max_retries: int = 1,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._transport = transport
        self._requests = None
        if transport is None:
            try:  # pragma: no cover - depends on environment
                import requests  # noqa: WPS433

                self._requests = requests
            except ImportError:  # pragma: no cover
                self._requests = None

    # -- public API --------------------------------------------------------

    def get(
        self,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout)

    def post(
        self,
        url: str,
        data: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        return self.request(
            "POST", url, params=params, data=data, headers=headers, timeout=timeout
        )

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a request, retrying transient failures.

        Never raises for network problems: failures come back as a response with
        ``error`` set, so a single bad provider cannot crash the pipeline.

        ``allow_redirects=False`` leaves the 3xx response to the caller, which is
        what :meth:`fetch` needs in order to walk and limit the chain itself.
        """
        merged_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            merged_headers.update(headers)
        effective_timeout = timeout if timeout is not None else self.timeout

        last: Optional[HttpResponse] = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                if self._transport is not None:
                    response = self._transport(
                        method, url, params, data, merged_headers, effective_timeout
                    )
                elif self._requests is not None:  # pragma: no cover - network path
                    response = self._via_requests(
                        method,
                        url,
                        params,
                        data,
                        merged_headers,
                        effective_timeout,
                        allow_redirects,
                    )
                else:  # pragma: no cover - network path
                    response = self._via_urllib(
                        method,
                        url,
                        params,
                        data,
                        merged_headers,
                        effective_timeout,
                        allow_redirects,
                    )
            except HttpError as exc:
                last = HttpResponse(status_code=0, url=url, error=str(exc))
                log.debug("HTTP %s %s failed (attempt %s): %s", method, url, attempt, exc)
                continue
            if not response.elapsed_seconds:
                response.elapsed_seconds = time.perf_counter() - started
            if response.error and attempt < self.max_retries:
                last = response
                continue
            return response
        return last or HttpResponse(status_code=0, url=url, error="request failed")

    # -- redirect aware fetch ---------------------------------------------

    def fetch(
        self,
        url: str,
        max_redirects: int = 5,
        max_bytes: int = 1_000_000,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        """Fetch ``url`` following redirects, with hard safety limits.

        ``requests`` and ``urllib`` both follow redirects by default, but neither
        enforces a limit we can report on. This method walks the chain manually
        so a redirect loop or an endless chain becomes a classified error rather
        than a hang, and so the final URL is known.

        The body is capped at ``max_bytes``; a longer body is truncated and
        flagged via ``truncated`` instead of being read into memory in full.
        """
        current = url
        seen: set[str] = set()
        redirects = 0
        merged_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        effective_timeout = timeout if timeout is not None else self.timeout
        total_elapsed = 0.0

        while True:
            if current in seen:
                return HttpResponse(
                    status_code=0,
                    url=current,
                    error="redirect loop detected",
                    elapsed_seconds=total_elapsed,
                )
            seen.add(current)

            response = self.request(
                "GET",
                current,
                headers=merged_headers,
                timeout=effective_timeout,
                allow_redirects=False,
            )
            total_elapsed += response.elapsed_seconds or 0.0
            if response.error is not None:
                response.elapsed_seconds = total_elapsed
                return response

            if not self._is_redirect(response.status_code):
                response.elapsed_seconds = total_elapsed
                if len(response.text or "") > max_bytes:
                    response.text = response.text[:max_bytes]
                    response.truncated = True
                return response

            location = self._header(response, "location")
            if not location:
                # A 3xx without a target is not a usable redirect; report it as
                # an error so the caller does not treat it as a live site.
                return HttpResponse(
                    status_code=response.status_code,
                    url=current,
                    error="redirect without a Location header",
                    elapsed_seconds=total_elapsed,
                )

            redirects += 1
            if redirects > max_redirects:
                return HttpResponse(
                    status_code=0,
                    url=current,
                    error=f"too many redirects (limit {max_redirects})",
                    elapsed_seconds=total_elapsed,
                )
            current = urljoin(current, location)

    def _is_redirect(self, status_code: int) -> bool:
        return status_code in (301, 302, 303, 307, 308)

    @staticmethod
    def _header(response: HttpResponse, name: str) -> Optional[str]:
        for key, value in (response.headers or {}).items():
            if key.lower() == name:
                return value
        return None

    # -- transports --------------------------------------------------------

    def _via_requests(  # pragma: no cover - network path
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]],
        data: Optional[Mapping[str, Any]],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        try:
            resp = self._requests.request(
                method,
                url,
                params=params,
                data=data,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
        except Exception as exc:  # requests.RequestException and friends
            raise HttpError(str(exc)) from exc
        return HttpResponse(
            status_code=resp.status_code,
            text=resp.text or "",
            headers={k.lower(): v for k, v in resp.headers.items()},
            url=str(resp.url),
        )

    def _via_urllib(  # pragma: no cover - network path
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]],
        data: Optional[Mapping[str, Any]],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        import urllib.error
        import urllib.parse
        import urllib.request

        if params:
            encoded = urllib.parse.urlencode(params, doseq=True)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{encoded}"

        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")

        request = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in headers.items():
            request.add_header(key, value)

        # urllib follows redirects via its default opener. When the caller wants
        # to inspect the 3xx itself, install an opener that refuses to follow.
        opener = None
        if not allow_redirects:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    return None

            opener = urllib.request.build_opener(_NoRedirect)

        try:
            open_url = opener.open if opener is not None else urllib.request.urlopen
            with open_url(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return HttpResponse(
                    status_code=response.status,
                    text=response.read().decode(charset, errors="replace"),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    url=response.url,
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                text=exc.read().decode("utf-8", errors="replace") if exc.fp else "",
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                url=url,
            )
        except Exception as exc:
            raise HttpError(str(exc)) from exc


__all__ = [
    "HttpClient",
    "HttpResponse",
    "HttpError",
    "ResponseTooLarge",
    "RedirectLimitExceeded",
    "DEFAULT_USER_AGENT",
]
