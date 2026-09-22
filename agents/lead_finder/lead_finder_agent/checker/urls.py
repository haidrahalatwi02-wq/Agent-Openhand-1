"""Safe URL handling for website checking.

A business website URL may come from a provider record, and a provider can put
almost anything in that field. The rules here exist to make sure a malformed or
hostile value is *rejected* rather than turned into a request:

- only ``http`` and ``https`` are ever requested, so ``javascript:``, ``file:``,
  ``data:``, ``ftp:`` and friends can never reach the HTTP client;
- a host is required, and it must look like a hostname, so free text such as
  ``"call us"`` or ``"N/A"`` is refused instead of being coerced into
  ``https://call us`` and sent to the network;
- the URL must not carry credentials, which stops a value like
  ``https://user:pass@host`` from being transmitted as a request target;
- length is capped so a pathological value cannot be used to blow up logs or
  header buffers.

The functions are pure and never raise for bad input; they return ``None`` so
the caller can classify the failure. Nothing here performs I/O.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

#: Only these schemes are ever fetched.
ALLOWED_SCHEMES = ("http", "https")

#: Longest URL accepted, matching common proxy/browser limits.
MAX_URL_LENGTH = 2048

#: A host must be a dotted name or a literal IP address. This is deliberately
#: stricter than the URL grammar: ``https://n/a`` parses but is not a website.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: Placeholder values providers use to mean "nothing here".
_NULLISH = {
    "n/a",
    "na",
    "none",
    "null",
    "nil",
    "unknown",
    "-",
    "--",
    "?",
    "no",
    "no website",
    "nosite",
    "not available",
    "non",
    "لا",
    "لا يوجد",
    "غير متوفر",
}


def _strip_control(value: str) -> str:
    """Drop control characters and whitespace that break header/request parsing."""
    return "".join(ch for ch in value if ch.isprintable() and not ch.isspace())


def looks_nullish(value: Optional[str]) -> bool:
    """True when a website field holds a placeholder rather than an address."""
    if value is None:
        return True
    text = str(value).strip().lower()
    return text == "" or text in _NULLISH


def _is_host_port_prefix(token: str) -> bool:
    """True when ``token`` before a ``:`` is really a host, not a scheme.

    ``example.com:8080`` and ``192.168.1.1:80`` are host:port pairs, while
    ``javascript:`` and ``mailto:`` are schemes. Schemes stay short and contain
    no dots, so a dotted token is treated as a host.
    """
    return "." in token


def _is_valid_host(host: str) -> bool:
    """Whether ``host`` is a plausible DNS name or IP literal."""
    candidate = host.strip().strip(".").lower()
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    if _IPV4_RE.match(candidate):
        # Structurally IPv4 but out of range (e.g. 999.1.1.1).
        return all(0 <= int(part) <= 255 for part in candidate.split("."))
    return bool(_HOSTNAME_RE.match(candidate))


def validate_website_url(value: Optional[str]) -> Optional[str]:
    """Return a safe absolute URL, or ``None`` if ``value`` is not one.

    A bare host such as ``example.com`` is accepted and upgraded to HTTPS,
    because providers commonly omit the scheme. Free text, unsupported
    schemes, credential-bearing URLs and non-web hosts are refused.
    """
    if looks_nullish(value):
        return None

    text = _strip_control(str(value))
    if not text or len(text) > MAX_URL_LENGTH:
        return None

    # Reject a scheme we do not serve *before* adding one, otherwise
    # "javascript:alert(1)" would become "https://javascript:alert(1)".
    # A URL scheme may legally contain dots, so "example.com:8080" must not be
    # mistaken for one: a dotted token before the colon is a host, and a value
    # with no "//" whose prefix looks like a host keeps its host:port form.
    scheme_match = re.match(r"^([a-z][a-z0-9+.-]*):", text, re.IGNORECASE)
    if scheme_match and _is_host_port_prefix(scheme_match.group(1)):
        # "example.com:8080/x" is a host:port rather than a scheme, so give it a
        # scheme. A malformed value may also carry leading slashes.
        host_part = text.lstrip("/")
        text = f"https://{host_part}"
    elif scheme_match:
        scheme = scheme_match.group(1).lower()
        if scheme not in ALLOWED_SCHEMES:
            return None
        rest = text[len(scheme) + 1 :]
        # "https:example.com" is a common malformed form; treat it as host-only.
        if not rest.startswith("//"):
            text = f"{scheme}://{rest.lstrip('/')}"
    else:
        text = f"https://{text.lstrip('/')}"

    try:
        parts = urlsplit(text)
    except ValueError:
        return None

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return None
    if parts.username or parts.password:
        # Credentials in a URL are never needed here and must not be sent.
        return None

    host = parts.hostname or ""
    if not _is_valid_host(host):
        return None

    try:
        port = parts.port  # raises ValueError when the port is out of range
    except ValueError:
        return None

    netloc = host.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""

    normalized = urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))
    return normalized or None


def safe_normalize_url(value: Optional[str]) -> Optional[str]:
    """Backwards-compatible alias used by the checker.

    Kept separate from :func:`lead_finder_agent.models.normalize_url`, which is
    a lenient formatter used while ingesting provider data and is intentionally
    allowed to accept values this function rejects.
    """
    return validate_website_url(value)


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_URL_LENGTH",
    "validate_website_url",
    "safe_normalize_url",
    "looks_nullish",
]
