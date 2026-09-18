"""Agents built on top of the Agent Core.

The Lead Finder lives in :mod:`lead_finder_agent.core.agent` because it owns the
pipeline. Every agent added after it lives here, so the coordination layer and
the agents it coordinates stay separate: ``core`` defines the contract and the
manager, ``agents`` implements the work.

Each agent is registered with the manager by name and shares its
:class:`~lead_finder_agent.core.agent.AgentContext`, so agents coordinate through
one repository instead of importing each other.
"""

from lead_finder_agent.agents.website_analyzer import (
    ATTENTION_SEVERITIES,
    INCONCLUSIVE_STATUSES,
    SEVERITY_ORDER,
    WebsiteAnalysis,
    WebsiteAnalyzerAgent,
    WebsiteFinding,
    load_analysis_config,
)

__all__ = [
    "WebsiteAnalyzerAgent",
    "WebsiteAnalysis",
    "WebsiteFinding",
    "load_analysis_config",
    "SEVERITY_ORDER",
    "ATTENTION_SEVERITIES",
    "INCONCLUSIVE_STATUSES",
]
