"""Website checking: does this business have a usable website?

The checker is independent of the search providers. Providers answer "what
businesses exist?"; the checker answers "does this business have a reachable
website?". A provider never classifies a website, and the checker never searches
for businesses, so either side can be replaced without touching the other.
"""

from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.checker.errors import classify_error, humanize_error
from lead_finder_agent.checker.http_checker import HttpWebsiteChecker, WebsiteSignals
from lead_finder_agent.checker.osm_tags import website_from_tags
from lead_finder_agent.checker.registry import (
    available_checkers,
    create_checker,
    register_checker,
)
from lead_finder_agent.checker.urls import validate_website_url

__all__ = [
    "BaseWebsiteChecker",
    "HttpWebsiteChecker",
    "WebsiteSignals",
    "website_from_tags",
    "classify_error",
    "humanize_error",
    "validate_website_url",
    "register_checker",
    "create_checker",
    "available_checkers",
]
