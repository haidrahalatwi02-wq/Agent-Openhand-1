"""Lead Finder Agent - Intelligent lead discovery and qualification system."""

__version__ = "1.0.0"
__author__ = "Lead Finder Team"
__description__ = "An intelligent lead discovery system using AI and location-based data"

from lead_finder_agent.core.agent import LeadFinderAgent
from lead_finder_agent.models.lead import Lead
from lead_finder_agent.models.search import SearchQuery

__all__ = ["LeadFinderAgent", "Lead", "SearchQuery"]
