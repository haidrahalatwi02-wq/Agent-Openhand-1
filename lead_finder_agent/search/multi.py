"""Runs several providers and aggregates their raw results."""

from __future__ import annotations

from typing import List, Sequence

from lead_finder_agent.models import ProviderResponse, SearchQuery, SearchResult
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.multi")


class MultiProviderSearch:
    """Fans a query out to every configured provider.

    Results are concatenated into a single raw list in provider order; per
    provider errors are preserved on :class:`SearchResult` so the caller can
    report partial failures without losing the successful results.

    **Limit handling.** Every provider is asked for the full ``limit``, and the
    final cap is applied by the caller *after* de-duplication. The reason is
    that this stage sees raw records and cannot know which of them are the same
    business: spending a shared budget on earlier providers would let their
    duplicates starve the providers that follow, silently costing unique leads.
    Asking each provider for the limit over-fetches slightly, which is the
    cheaper of the two mistakes; the providers cap themselves internally, and
    :class:`~lead_finder_agent.core.pipeline.LeadFinderPipeline` trims the
    merged result back to ``limit``.
    """

    def __init__(self, providers: Sequence[BaseSearchProvider]) -> None:
        self.providers: List[BaseSearchProvider] = list(providers)

    def run(self, query: SearchQuery) -> SearchResult:
        """Query every provider and collect the raw records."""
        responses: List[ProviderResponse] = []
        raw_leads: List[dict] = []

        for provider in self.providers:
            response = provider.search(self._provider_query(query))
            responses.append(response)

            if response.ok:
                raw_leads.extend(response.leads)
            if response.error:
                log.warning("Provider %s error: %s", provider.name, response.error)
            elif response.skipped_reason:
                # Only reached when there is no error: a failed provider already
                # carries a skip reason, and logging both would report the same
                # event twice at two different severities.
                log.info("Provider %s skipped: %s", provider.name, response.skipped_reason)

        return SearchResult(query=query, leads=raw_leads, responses=responses)

    @staticmethod
    def _provider_query(query: SearchQuery) -> SearchQuery:
        """A per-provider copy of ``query``.

        A copy is passed rather than the original so a provider cannot mutate
        the shared query, and so every provider sees the same location and
        limit.
        """
        return SearchQuery(
            business_type=query.business_type,
            keywords=list(query.keywords),
            city=query.city,
            country=query.country,
            limit=query.limit,
            providers=query.providers,
            language=query.language,
        )

    def __len__(self) -> int:
        return len(self.providers)


__all__ = ["MultiProviderSearch"]
