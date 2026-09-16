"""Text and parsing helpers used across modules."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Optional
from urllib.parse import urlparse

_WHITESPACE = re.compile(r"\s+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

#: Below this many digits a phone match is too weak to be conclusive.
_MIN_COMPARABLE_PHONE_DIGITS = 7

#: ~110 metres of latitude; see :func:`coordinates_close`.
_DEFAULT_COORD_TOLERANCE = 0.001

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


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
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


# --------------------------------------------------------------------------- #
# Comparison helpers
#
# These exist for *matching*, never for display. Nothing here is allowed to
# change what a provider reported: the normalizer keeps the original text and
# only these functions reduce a value to a comparable form.
# --------------------------------------------------------------------------- #

#: Arabic letters that differ only by orthography or by an optional diacritic.
#: Folding them makes ``أحمد`` and ``احمد`` compare equal, which matters because
#: volunteers and APIs transliterate inconsistently.
_ARABIC_FOLD = str.maketrans(
    {
        "\u0623": "\u0627",  # أ -> ا
        "\u0625": "\u0627",  # إ -> ا
        "\u0622": "\u0627",  # آ -> ا
        "\u0649": "\u064a",  # ى -> ي
        "\u0629": "\u0647",  # ة -> ه
        "\u0640": "",        # tatweel (kashida) is purely decorative
    }
)
_ARABIC_UNDIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")

#: Legal-form words carry no identifying information ("... LLC" vs "... L.L.C.").
_LEGAL_SUFFIXES = frozenset(
    {
        "llc", "ltd", "limited", "inc", "incorporated", "co", "company",
        "corp", "corporation", "plc", "gmbh", "sarl", "srl", "bv", "nv",
        "est", "establishment", "wll", "shpk",
        # Arabic legal forms, matched after folding.
        "ش.م.م", "ش.م.ب", "ذ.م.م", "م.م", "مؤسسه", "شركه",
    }
)

#: Address words that only describe a building type, not a location.
_ADDRESS_NOISE = frozenset(
    {
        "building", "bldg", "floor", "flr", "flat", "apartment", "apt", "suite",
        "office", "shop", "unit", "block", "tower", "complex", "mall", "center",
        "centre", "plaza", "street", "st", "road", "rd", "avenue", "ave",
        "boulevard", "blvd", "lane", "ln", "drive", "dr", "highway", "hwy",
        "p.o", "po", "box", "pobox",
        # Arabic equivalents, matched after folding.
        "شارع", "جاده", "طريق", "مبنى", "عماره", "برج", "مجمع", "حي", "منطقه",
    }
)


def fold_arabic(value: str) -> str:
    """Fold Arabic orthographic variants and drop diacritics."""
    text = str(value).translate(_ARABIC_FOLD)
    return _ARABIC_UNDIACRITICS.sub("", text)


def comparable_text(value: Optional[str], drop_tokens: frozenset = frozenset()) -> str:
    """Reduce free text to a lowercase, punctuation-free, comparable form.

    Punctuation becomes a space rather than being removed, so ``Al-Bahr`` and
    ``Al Bahr`` agree instead of collapsing to ``albahr`` and ``al bahr``.
    """
    if not value:
        return ""
    text = normalize_whitespace(str(value).lower()) or ""
    text = fold_arabic(text)
    letters = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    tokens = [token for token in letters.split() if token]
    if drop_tokens:
        stripped = [token for token in tokens if token not in drop_tokens]
        tokens = stripped or tokens
    return " ".join(tokens)


def comparable_name(value: Optional[str]) -> str:
    """A business name reduced to its identifying words."""
    return comparable_text(value, _LEGAL_SUFFIXES)


def comparable_address(value: Optional[str]) -> str:
    """An address reduced to comparable form, without street-type noise."""
    return comparable_text(value, _ADDRESS_NOISE)


def name_similarity(first: Optional[str], second: Optional[str]) -> float:
    """Similarity of two business names on a 0..1 scale.

    Returns ``1.0`` for two non-empty names that are equal once normalized.
    """
    a = comparable_name(first)
    b = comparable_name(second)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def same_phone(first: Optional[str], second: Optional[str]) -> bool:
    """Whether two phone numbers are the same, ignoring formatting.

    A short number is treated as unusable rather than conclusive: local
    emergency-style numbers and partial entries collide by accident.
    """
    a = re.sub(r"\D", "", str(first or ""))
    b = re.sub(r"\D", "", str(second or ""))
    return len(a) >= _MIN_COMPARABLE_PHONE_DIGITS and a == b


def same_domain(first: Optional[str], second: Optional[str]) -> bool:
    """Whether two URLs share a registrable domain."""
    a = registrable_domain(first)
    b = registrable_domain(second)
    return bool(a and b and a == b)


def coordinates_close(
    first_lat: Optional[float],
    first_lon: Optional[float],
    second_lat: Optional[float],
    second_lon: Optional[float],
    tolerance: float = _DEFAULT_COORD_TOLERANCE,
) -> bool:
    """Whether two points are within ``tolerance`` degrees of each other.

    A degree of latitude is ~111 km, so the default ~0.001 (about 110 m) is
    tight enough that two neighbouring shopfronts stay distinct.
    """
    if None in (first_lat, first_lon, second_lat, second_lon):
        return False
    try:
        return (
            abs(float(first_lat) - float(second_lat)) <= tolerance
            and abs(float(first_lon) - float(second_lon)) <= tolerance
        )
    except (TypeError, ValueError):
        return False


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
    "safe_float",
    "extract_domain",
    "registrable_domain",
    "fold_arabic",
    "comparable_text",
    "comparable_name",
    "comparable_address",
    "name_similarity",
    "same_phone",
    "same_domain",
    "coordinates_close",
]
