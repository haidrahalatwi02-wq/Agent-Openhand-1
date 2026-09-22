"""The ``LeadScorer`` contract.

Scoring is deliberately isolated behind this interface: the pipeline depends on
:class:`BaseLeadScorer`, never on a concrete rule engine, so a different scoring
strategy can be substituted without touching the core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Mapping, Optional

from lead_finder_agent.models import Lead, LeadScore


class BaseLeadScorer(ABC):
    """Turn a normalized lead into a structured :class:`LeadScore`.

    A scorer receives the lead together with the lead's
    :class:`~lead_finder_agent.models.WebsiteCheckResult` when one is available.
    The check is passed explicitly as well as being stored on the lead because a
    caller may be re-scoring stored records offline, where the freshest check
    only exists in memory.

    Implementations must be **deterministic**: the same lead and check must always
    produce the same score, reasons and breakdown. They must not call an LLM, use
    randomness, or read the wall clock — a score has to be reproducible after the
    fact.
    """

    @abstractmethod
    def score(self, lead: Lead, check: Optional[object] = None) -> LeadScore:
        """Return the score for ``lead`` without modifying it."""

    @abstractmethod
    def score_lead(self, lead: Lead, check: Optional[object] = None) -> Lead:
        """Score ``lead``, apply the result to it, and return the same object."""

    @abstractmethod
    def score_all(
        self, leads: Iterable[Lead], checks: Optional[Mapping[str, object]] = None
    ) -> List[Lead]:
        """Score every lead, pairing each with its check from ``checks``."""


__all__ = ["BaseLeadScorer"]
