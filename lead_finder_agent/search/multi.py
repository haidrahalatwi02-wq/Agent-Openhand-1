"""Runs several providers and aggregates their raw results."""

from __future__ import annotations

from typing import List, Sequence

from lead_finder_agent.models import ProviderResponse, SearchQuery, SearchResult
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.multi")


class MultiProviderSearch:
    """Fans a query out to every configured provider.

    Results are concatenated into a single raw list; per-provider errors are
    preserved on :class:`SearchResult` so the caller can report partial
    failures without losing the successful results.
    """

    def __init__(self, providers: Sequence[BaseSearchProvider]) -> None:
        self.providers: List[BaseSearchProvider] = list(providers)

    def run(self, query: SearchQuery) -> SearchResult:
        """Query every provider and collect the raw records."""
        responses: List[ProviderResponse] = []
        raw_leads: List[dict] = []
        remaining = query.limit

        for provider in self.providers:
            provider_query = query
            if remaining <= 0:
                break
            # Ask each provider for at most what we still need.
            if provider is not self.providers[0] or len(self.providers) == 1:
                provider_query = SearchQuery(
                    business_type=query.business_type,
                    keywords=list(query.keywords),
                    city=query.city,
                    country=query.country,
                    limit=remaining,
                    providers=query.providers,
                    language=query.language,
                )

            response = provider.search(provider_query)
            responses.append(response)

            if response.ok:
                raw_leads.extend(response.leads)
                remaining = query.limit - len(raw_leads)
            if response.error:
                log.warning("Provider %s error: %s", provider.name, response.error)
            elif response.skipped_reason:
                # Only reached when there is no error: a failed provider already
                # carries a skip reason, and logging both would report the same
                # event twice at two different severities.
                log.info("Provider %s skipped: %s", provider.name, response.skipped_reason)

        return SearchResult(query=query, leads=raw_leads, responses=responses)

    def __len__(self) -> int:
        return len(self.providers)


__all__ = ["MultiProviderSearch"]
