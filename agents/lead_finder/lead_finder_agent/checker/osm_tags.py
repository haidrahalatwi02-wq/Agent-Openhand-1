"""Helpers for reading website hints out of OpenStreetMap-style tags."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

_WEBSITE_KEYS = ("website", "contact:website", "url", "website:official", "website:menu")
_SOCIAL_KEYS = (
    "contact:facebook",
    "facebook",
    "contact:instagram",
    "instagram",
    "contact:twitter",
    "contact:whatsapp",
)

SOCIAL_HOSTS = (
    "facebook.",
    "fb.com",
    "instagram.",
    "twitter.",
    "x.com",
    "tiktok.",
    "linkedin.",
    "youtube.",
    "wa.me",
    "whatsapp.",
    "t.me",
    "telegram.",
)


def is_social_url(url: Optional[str]) -> bool:
    """True when ``url`` points at a social platform rather than an owned site."""
    if not url:
        return False
    lowered = str(url).lower()
    return any(host in lowered for host in SOCIAL_HOSTS)


def website_from_tags(tags: Mapping[str, Any]) -> Tuple[Optional[str], Dict[str, str]]:
    """Split website and social links out of a tag mapping.

    Returns ``(website_url, social_links)``. A social URL stored in the website
    tag is moved to ``social_links`` rather than being treated as a website.
    """
    website: Optional[str] = None
    for key in _WEBSITE_KEYS:
        value = tags.get(key)
        if value:
            website = str(value)
            break

    social: Dict[str, str] = {}
    for key in _SOCIAL_KEYS:
        value = tags.get(key)
        if value:
            social[key.split(":")[-1]] = str(value)

    if website and is_social_url(website):
        label = "social"
        for host, name in (
            ("facebook", "facebook"),
            ("instagram", "instagram"),
            ("twitter", "twitter"),
            ("x.com", "twitter"),
            ("tiktok", "tiktok"),
            ("whatsapp", "whatsapp"),
            ("wa.me", "whatsapp"),
        ):
            if host in website.lower():
                label = name
                break
        social.setdefault(label, website)
        website = None

    return website, social


__all__ = ["website_from_tags", "is_social_url", "SOCIAL_HOSTS"]
