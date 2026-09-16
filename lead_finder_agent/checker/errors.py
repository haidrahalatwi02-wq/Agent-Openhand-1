"""Classify transport failures into a structured error taxonomy.

The HTTP layer reports failures as free text because that is all a transport can
reliably provide, and different libraries word the same problem differently. The
patterns below map that text onto :class:`WebsiteErrorKind` so callers can branch
on a stable value and the CLI can print something short and safe.

Classification is intentionally conservative: anything unrecognised becomes
``UNKNOWN`` rather than being guessed at.
"""

from __future__ import annotations

import re
from typing import Optional

from lead_finder_agent.models import WebsiteErrorKind

# Ordered most specific first: TLS messages often also mention "connection",
# and DNS messages often also mention "name", so the first match must win.
_PATTERNS = (
    (
        WebsiteErrorKind.REDIRECT_LIMIT,
        re.compile(r"too many redirect|redirect loop|redirect without|exceeded.*redirect", re.I),
    ),
    (
        WebsiteErrorKind.RESPONSE_TOO_LARGE,
        re.compile(r"too large|response size|content length|exceeds.*size|body.*limit", re.I),
    ),
    (
        WebsiteErrorKind.TIMEOUT,
        re.compile(r"timed?\s*out|timeout|read timeout|connect timeout", re.I),
    ),
    (
        WebsiteErrorKind.TLS_FAILURE,
        re.compile(
            r"ssl|certificate|tls|handshake|CERT_|self[- ]signed|hostname mismatch|"
            r"wrong version number|unable to get local issuer",
            re.I,
        ),
    ),
    (
        WebsiteErrorKind.DNS_FAILURE,
        re.compile(
            r"name or service not known|nodename nor servname|getaddrinfo|dns|"
            r"no address associated|name resolution|temporary failure in name",
            re.I,
        ),
    ),
    (
        WebsiteErrorKind.CONNECTION_FAILURE,
        re.compile(
            r"connection|refused|reset by peer|unreachable|network is down|"
            r"broken pipe|eof occurred|remote end closed",
            re.I,
        ),
    ),
    (
        WebsiteErrorKind.INVALID_URL,
        re.compile(r"invalid url|malformed url|unsupported scheme|not a valid", re.I),
    ),
    (
        WebsiteErrorKind.MALFORMED_RESPONSE,
        re.compile(r"malformed|invalid header|bad status line|decode|charset", re.I),
    ),
)

#: Short, user-facing phrasing. No exception text ever reaches the CLI.
_HUMAN = {
    WebsiteErrorKind.INVALID_URL: "invalid website URL",
    WebsiteErrorKind.TIMEOUT: "connection timed out",
    WebsiteErrorKind.DNS_FAILURE: "domain name could not be resolved",
    WebsiteErrorKind.TLS_FAILURE: "secure connection failed",
    WebsiteErrorKind.CONNECTION_FAILURE: "could not connect",
    WebsiteErrorKind.HTTP_ERROR: "server returned an error",
    WebsiteErrorKind.REDIRECT_LIMIT: "too many redirects",
    WebsiteErrorKind.RESPONSE_TOO_LARGE: "response too large",
    WebsiteErrorKind.MALFORMED_RESPONSE: "invalid response from server",
    WebsiteErrorKind.UNKNOWN: "could not determine website",
}


def classify_error(message: Optional[str], http_status: Optional[int] = None) -> WebsiteErrorKind:
    """Map a transport error string onto a structured kind.

    ``http_status`` is used only when there is no transport error, so a 5xx that
    still returned headers is reported as an HTTP error.
    """
    if message:
        text = str(message)
        for kind, pattern in _PATTERNS:
            if pattern.search(text):
                return kind
    if http_status is not None and http_status >= 400:
        return WebsiteErrorKind.HTTP_ERROR
    return WebsiteErrorKind.UNKNOWN


def humanize_error(kind: WebsiteErrorKind) -> str:
    """Return the short public phrasing for ``kind``."""
    return _HUMAN.get(kind, _HUMAN[WebsiteErrorKind.UNKNOWN])


__all__ = ["classify_error", "humanize_error"]