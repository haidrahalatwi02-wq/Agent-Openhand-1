"""HTTP client with a pluggable transport.

Using ``requests`` when it is installed and falling back to the standard library
``urllib`` keeps the package installable in restricted environments. Tests
inject a fake transport so no network access is required.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("utils.http")

DEFAULT_USER_AGENT = (
    "LeadFinderAgent/0.1 (+https://github.com/haidrahalatwi02-wq/Agent-Openhand-1)"
)


class HttpError(Exception):
    """Raised for transport level failures (connection, DNS, timeout)."""


@dataclass
class HttpResponse:
    """Normalized HTTP response independent of the underlying library."""

    status_code: int
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    error: Optional[str] = None

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
    ) -> HttpResponse:
        """Perform a request, retrying transient failures.

        Never raises for network problems: failures come back as a response with
        ``error`` set, so a single bad provider cannot crash the pipeline.
        """
        merged_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            merged_headers.update(headers)
        effective_timeout = timeout if timeout is not None else self.timeout

        last: Optional[HttpResponse] = None
        for attempt in range(self.max_retries + 1):
            try:
                if self._transport is not None:
                    response = self._transport(
                        method, url, params, data, merged_headers, effective_timeout
                    )
                elif self._requests is not None:  # pragma: no cover - network path
                    response = self._via_requests(
                        method, url, params, data, merged_headers, effective_timeout
                    )
                else:  # pragma: no cover - network path
                    response = self._via_urllib(
                        method, url, params, data, merged_headers, effective_timeout
                    )
            except HttpError as exc:
                last = HttpResponse(status_code=0, url=url, error=str(exc))
                log.debug("HTTP %s %s failed (attempt %s): %s", method, url, attempt, exc)
                continue
            if response.error and attempt < self.max_retries:
                last = response
                continue
            return response
        return last or HttpResponse(status_code=0, url=url, error="request failed")

    # -- transports --------------------------------------------------------

    def _via_requests(  # pragma: no cover - network path
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]],
        data: Optional[Mapping[str, Any]],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        try:
            resp = self._requests.request(
                method,
                url,
                params=params,
                data=data,
                headers=dict(headers),
                timeout=timeout,
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

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
                url=url,
            )
        except Exception as exc:
            raise HttpError(str(exc)) from exc


__all__ = ["HttpClient", "HttpResponse", "HttpError", "DEFAULT_USER_AGENT"]
