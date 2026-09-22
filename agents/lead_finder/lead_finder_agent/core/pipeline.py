"""The Lead Finder pipeline.

    Input -> Search -> Normalize -> Deduplicate -> Website Check -> Score -> Storage -> Results

Each stage is a method so it can be overridden or skipped by subclasses, and
every external dependency (providers, checker, scorer, repository) is injected.
That is what makes the pipeline testable offline and extensible later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.extraction.deduplicator import Deduplicator
from lead_finder_agent.extraction.normalizer import LeadNormalizer
from lead_finder_agent.models import (
    Lead,
    SearchQuery,
    SearchResult,
    WebsiteStatus,
    utcnow,
)
from lead_finder_agent.scoring.base import BaseLeadScorer
from lead_finder_agent.scoring.engine import LeadScorer
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.search.multi import MultiProviderSearch
from lead_finder_agent.storage.base import BaseLeadRepository
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("core.pipeline")


@dataclass
class PipelineStats:
    """Counters describing what happened during a run."""

    query: Optional[SearchQuery] = None
    providers: List[Dict[str, Any]] = field(default_factory=list)
    raw_count: int = 0
    normalized_count: int = 0
    duplicates_removed: int = 0
    checked_count: int = 0
    scored_count: int = 0
    stored_count: int = 0
    failed_stages: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query.to_dict() if self.query else None,
            "providers": list(self.providers),
            "raw_count": self.raw_count,
            "normalized_count": self.normalized_count,
            "duplicates_removed": self.duplicates_removed,
            "checked_count": self.checked_count,
            "scored_count": self.scored_count,
            "stored_count": self.stored_count,
            "failed_stages": list(self.failed_stages),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class PipelineResult:
    """Everything a run produced."""

    query: SearchQuery
    leads: List[Lead] = field(default_factory=list)
    search_result: Optional[SearchResult] = None
    stats: PipelineStats = field(default_factory=PipelineStats)

    @property
    def count(self) -> int:
        return len(self.leads)

    @property
    def errors(self) -> Dict[str, str]:
        return self.search_result.errors if self.search_result else {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "stats": self.stats.to_dict(),
            "leads": [lead.to_dict() for lead in self.leads],
        }

    def top(self, limit: int = 10) -> List[Lead]:
        return sorted(self.leads, key=lambda l: l.sort_key(), reverse=True)[:limit]


class LeadFinderPipeline:
    """Runs the full discovery pipeline for a :class:`SearchQuery`."""

    def __init__(
        self,
        providers: Optional[Sequence[BaseSearchProvider]] = None,
        checker: Optional[BaseWebsiteChecker] = None,
        scorer: Optional[BaseLeadScorer] = None,
        repository: Optional[BaseLeadRepository] = None,
        normalizer: Optional[LeadNormalizer] = None,
        deduplicator: Optional[Deduplicator] = None,
        check_websites: bool = True,
        store_results: bool = True,
        max_checks: Optional[int] = None,
        default_country: Optional[str] = None,
        default_city: Optional[str] = None,
    ) -> None:
        self.providers = list(providers or [])
        self.checker = checker or HttpWebsiteChecker()
        self.scorer = scorer or LeadScorer()
        self.repository = repository
        self.normalizer = normalizer or LeadNormalizer(
            default_country=default_country, default_city=default_city
        )
        self.deduplicator = deduplicator or Deduplicator()
        self.check_websites = check_websites
        self.store_results = store_results
        self.max_checks = max_checks

    # -- pipeline stages ---------------------------------------------------

    def run(self, query: SearchQuery) -> PipelineResult:
        """Execute every stage and return the collected leads."""
        started = _now()
        stats = PipelineStats(query=query)
        result = PipelineResult(query=query, stats=stats)

        # 1. Search
        search_result = self.search(query, stats)
        result.search_result = search_result
        stats.raw_count = len(search_result.leads)

        # 2. Normalize
        leads = self.normalize(search_result, stats)

        # 3. Deduplicate
        leads = self.deduplicate(leads, stats)

        # 4. Website check
        if self.check_websites:
            leads = self.check_website(leads, stats)
        else:
            stats.failed_stages.append("website_check:skipped")

        # 5. Score
        leads = self.score(leads, stats)

        # 6. Storage
        if self.store_results and self.repository is not None:
            self.store(leads, stats)
        elif self.store_results:
            stats.failed_stages.append("storage:no_repository")
            log.warning("No repository configured; results were not persisted")

        # 7. Results
        leads = sorted(leads, key=lambda l: l.sort_key(), reverse=True)
        result.leads = leads
        stats.elapsed_seconds = _now() - started
        log.info(
            "Pipeline finished: %s leads (raw=%s, duplicates=%s, %.2fs)",
            len(leads),
            stats.raw_count,
            stats.duplicates_removed,
            stats.elapsed_seconds,
        )
        return result

    # -- individual stages -------------------------------------------------

    def search(self, query: SearchQuery, stats: PipelineStats) -> SearchResult:
        """Stage 1: ask every provider for raw records."""
        if not self.providers:
            log.warning("No search providers configured")
            stats.failed_stages.append("search:no_providers")
            return SearchResult(query=query)
        engine = MultiProviderSearch(self.providers)
        search_result = engine.run(query)
        stats.providers = [
            {
                "provider": r.provider,
                "kind": str(r.kind),
                "count": r.count,
                "error": r.error,
                "skipped_reason": r.skipped_reason,
                "elapsed_seconds": round(r.elapsed_seconds, 4),
            }
            for r in search_result.responses
        ]
        if search_result.errors:
            stats.failed_stages.append("search:provider_errors")
        return search_result

    def normalize(self, search_result: SearchResult, stats: PipelineStats) -> List[Lead]:
        """Stage 2: clean provider records into leads."""
        by_provider: Dict[str, List[Dict[str, Any]]] = {}
        for response in search_result.responses:
            if response.ok:
                by_provider.setdefault(response.provider, []).extend(response.leads)

        leads: List[Lead] = []
        if by_provider:
            for provider, raws in by_provider.items():
                leads.extend(self.normalizer.normalize_many(raws, source=provider))
        else:
            # Providers may return un-attributed records (e.g. custom callers).
            leads = self.normalizer.normalize_many(search_result.leads, source="unknown")

        stats.normalized_count = len(leads)
        return leads

    def deduplicate(self, leads: List[Lead], stats: PipelineStats) -> List[Lead]:
        """Stage 3: collapse duplicate businesses.

        The query limit is applied *here*, after duplicates are gone, so the
        user gets the number of distinct businesses they asked for. Applying it
        earlier would count duplicates against the budget and quietly return
        fewer unique leads than requested.
        """
        before = len(leads)
        unique = self.deduplicator.deduplicate(leads)
        stats.duplicates_removed = before - len(unique)

        limit = getattr(stats.query, "limit", None)
        if limit is not None and limit > 0 and len(unique) > limit:
            unique = unique[:limit]
        return unique

    def check_website(self, leads: List[Lead], stats: PipelineStats) -> List[Lead]:
        """Stage 4: determine whether each business has a website."""
        checked = 0
        for lead in leads:
            if self.max_checks is not None and checked >= self.max_checks:
                break
            try:
                result = self.checker.check(lead)
            except Exception as exc:  # noqa: BLE001 - isolate per-lead failures
                log.warning("Website check failed for %r: %s", lead.business_name, exc)
                stats.failed_stages.append("website_check:error")
                continue

            checked += 1
            lead.website_status = result.status
            lead.website_quality = result.quality
            lead.website_checked_at = result.checked_at
            lead.last_checked_at = utcnow()
            if result.website_url and result.status == WebsiteStatus.EXISTS:
                lead.website_url = result.website_url
            # Keep the check on the raw payload so scoring can read it back.
            lead.raw["website_check"] = result.to_dict()

        stats.checked_count = checked
        return leads

    def score(self, leads: List[Lead], stats: PipelineStats) -> List[Lead]:
        """Stage 5: assign score, confidence and reasons."""
        for lead in leads:
            self.scorer.score_lead(lead)
        stats.scored_count = len(leads)
        return leads

    def store(self, leads: List[Lead], stats: PipelineStats) -> int:
        """Stage 6: persist leads, skipping exact duplicates already stored."""
        if self.repository is None:
            return 0
        written = 0
        for lead in leads:
            self.repository.add(lead)
            written += 1
        stats.stored_count = written
        return written


def _now() -> float:
    import time

    return time.perf_counter()


__all__ = ["LeadFinderPipeline", "PipelineResult", "PipelineStats"]
