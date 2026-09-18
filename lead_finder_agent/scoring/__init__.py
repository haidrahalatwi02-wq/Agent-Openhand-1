"""Lead scoring: turn a lead's signals into a score, confidence and reasons."""

from lead_finder_agent.scoring.base import BaseLeadScorer
from lead_finder_agent.scoring.engine import (
    LeadScorer,
    build_signals,
    hydrate_website_check,
)
from lead_finder_agent.scoring.rules import ScoringRule, ScoringRules, load_scoring_rules

__all__ = [
    "BaseLeadScorer",
    "LeadScorer",
    "ScoringRule",
    "ScoringRules",
    "build_signals",
    "hydrate_website_check",
    "load_scoring_rules",
]
