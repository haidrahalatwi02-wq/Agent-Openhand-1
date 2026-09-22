"""Lead Finder Agent - find local businesses without a website and score them.

The public API is intentionally small: build an :class:`AgentCore` (or run the
CLI) and call :meth:`AgentCore.search`. Every internal component (search
providers, website checker, scoring, storage) is swappable.
"""

from lead_finder_agent.models import (
    Lead,
    LeadScore,
    SearchQuery,
    SearchResult,
    WebsiteCheckResult,
    WebsiteStatus,
)

__all__ = [
    "Lead",
    "LeadScore",
    "SearchQuery",
    "SearchResult",
    "WebsiteCheckResult",
    "WebsiteStatus",
]

__version__ = "0.1.0"
