"""Text and parsing helpers used across modules."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, List, Optional
from urllib.parse import urlparse

_WHITESPACE = re.compile(r"\s+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Very small public-suffix list. It only needs to cover the multi-label suffixes
# that appear in the countries this project targets by default; anything else
# falls back to the last two labels.
_MULTI_LABEL_SUFFIXES = (
    "co.uk",
    "org.uk",
    "ac.uk",
    "co.za",
    "com.au",
    "co.nz",
    "com.br",
    "co.jp",
    "com.sa",
    "co.ke",
    "com.tr",
    "com.eg",
)


def normalize_whitespace(value: Optional[str]) -> Optional[str]:
    """Collapse runs of whitespace and trim."""
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", str(value)).strip()
    return cleaned or None


def truncate(value: Optional[str], length: int = 200) -> Optional[str]:
    """Trim a string to ``length`` characters, adding an ellipsis."""
    text = normalize_whitespace(value)
    if text is None:
        return None
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)].rstrip() + "\u2026"


def slugify(value: Optional[str]) -> str:
    """ASCII, lowercase, hyphen separated version of ``value``."""
    if not value:
        return ""
    ascii_text = (
        unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    )
    return _SLUG_STRIP.sub("-", ascii_text.lower()).strip("-")


def parse_keywords(value: Any) -> List[str]:
    """Accept a string, an iterable, or ``None`` and return a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[str] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return [p.strip() for p in parts if p and p.strip()]


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Best-effort integer conversion."""
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def extract_domain(url: Optional[str]) -> Optional[str]:
    """Lowercase hostname of a URL, without ``www.``."""
    if not url:
        return None
    text = str(url).strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        text = "https://" + text.lstrip("/")
    host = urlparse(text).hostname
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url: Optional[str]) -> Optional[str]:
    """Best-effort registrable domain (``shop.example.co.uk`` -> ``example.co.uk``)."""
    host = extract_domain(url)
    if not host:
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return host
    for suffix in _MULTI_LABEL_SUFFIXES:
        if host.endswith("." + suffix):
            head = host[: -(len(suffix) + 1)]
            label = head.split(".")[-1] if head else ""
            return f"{label}.{suffix}" if label else host
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


__all__ = [
    "normalize_whitespace",
    "truncate",
    "slugify",
    "parse_keywords",
    "safe_int",
    "extract_domain",
    "registrable_domain",
]
