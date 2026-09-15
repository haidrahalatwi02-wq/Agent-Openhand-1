"""Agent Core: the coordination layer.

The Lead Finder is the first agent, but the project is designed so more agents
(Website Analyzer, Outreach, Follow-up, CRM, Reporting) can be added without
rebuilding anything. Two pieces make that possible:

* :class:`BaseAgent` - a tiny contract every agent implements: ``name``,
  ``description`` and ``run(**kwargs)``.
* :class:`AgentContext` - a shared bundle of dependencies (settings, repository,
  providers) that agents receive instead of constructing their own.

:class:`LeadFinderAgent` wires those together around
:class:`~lead_finder_agent.core.pipeline.LeadFinderPipeline`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.checker.http_checker import HttpWebsiteChecker
from lead_finder_agent.config.settings import Settings, get_settings
from lead_finder_agent.core.pipeline import LeadFinderPipeline, PipelineResult
from lead_finder_agent.extraction.deduplicator import Deduplicator
from lead_finder_agent.extraction.normalizer import LeadNormalizer
from lead_finder_agent.models import Lead, SearchQuery
from lead_finder_agent.scoring.engine import LeadScorer
from lead_finder_agent.scoring.rules import ScoringRules, load_scoring_rules
from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.search.registry import build_providers
from lead_finder_agent.storage.base import BaseLeadRepository
from lead_finder_agent.storage.exporters import export_leads
from lead_finder_agent.storage.sqlite_repository import SQLiteLeadRepository
from lead_finder_agent.utils.logging_utils import get_logger, setup_logging

log = get_logger("core.agent")


@dataclass
class AgentContext:
    """Dependencies shared by every agent in the system."""

    settings: Settings = field(default_factory=get_settings)
    repository: Optional[BaseLeadRepository] = None
    providers: Optional[Sequence[BaseSearchProvider]] = None
    checker: Optional[BaseWebsiteChecker] = None
    scorer: Optional[LeadScorer] = None
    normalizer: Optional[LeadNormalizer] = None
    deduplicator: Optional[Deduplicator] = None
    scoring_rules: Optional[ScoringRules] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def resolve_repository(self) -> BaseLeadRepository:
        """Return the repository, creating a SQLite one on first use."""
        if self.repository is None:
            self.settings.ensure_directories()
            self.repository = SQLiteLeadRepository(self.settings.db_path)
        return self.repository

    def resolve_providers(self, names: Optional[Sequence[str]] = None) -> List[BaseSearchProvider]:
        """Return providers.

        Precedence: explicit ``names`` > providers injected on the context >
        providers configured in settings. Injected providers matter because
        tests and callers use them to stay offline.
        """
        if self.providers is not None and not names:
            return list(self.providers)
        if names:
            return build_providers(list(names), config=self._provider_config())
        return build_providers(list(self.settings.providers), config=self._provider_config())

    def _provider_config(self) -> Dict[str, Any]:
        settings = self.settings
        return {
            "user_agent": settings.user_agent,
            "http_timeout": settings.http_timeout,
            "max_retries": settings.max_retries,
            "overpass_url": settings.overpass_url,
            "nominatim_url": settings.nominatim_url,
            "default_country": settings.default_country,
            "default_city": settings.default_city,
            "business_types_path": settings.business_types_path,
        }

    def resolve_checker(self) -> BaseWebsiteChecker:
        if self.checker is None:
            settings = self.settings
            self.checker = HttpWebsiteChecker(
                config={
                    "user_agent": settings.user_agent,
                    "http_timeout": settings.http_timeout,
                    "max_retries": 0,
                }
            )
        return self.checker

    def resolve_scorer(self) -> LeadScorer:
        if self.scorer is None:
            rules = self.scoring_rules or load_scoring_rules(self.settings.scoring_rules_path)
            self.scorer = LeadScorer(rules=rules)
        return self.scorer

    def close(self) -> None:
        if self.repository is not None:
            close = getattr(self.repository, "close", None)
            if callable(close):
                close()


class BaseAgent(abc.ABC):
    """Contract for every agent in the system.

    Keep implementations small and single-purpose; coordinate through
    :class:`AgentContext` instead of importing other agents directly.
    """

    name: str = "agent"
    description: str = ""

    def __init__(self, context: Optional[AgentContext] = None) -> None:
        self.context = context or AgentContext()

    @abc.abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the agent's task."""

    def info(self) -> Dict[str, str]:
        return {"name": self.name, "description": self.description}

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        return f"<{type(self).__name__} name={self.name!r}>"


class LeadFinderAgent(BaseAgent):
    """Finds businesses, checks their websites and scores them as leads.

    This is agent #1. Adding another agent means creating a new
    :class:`BaseAgent` subclass and registering it with the manager you build
    next; the pipeline and this class stay untouched.
    """

    name = "lead_finder"
    description = "Discover local businesses, check their web presence and score leads"

    def __init__(
        self,
        context: Optional[AgentContext] = None,
        check_websites: bool = True,
        store_results: bool = True,
        max_checks: Optional[int] = None,
    ) -> None:
        super().__init__(context)
        self.check_websites = check_websites
        self.store_results = store_results
        self.max_checks = max_checks
        setup_logging(self.context.settings.log_level)

    # -- main entry point --------------------------------------------------

    def run(
        self,
        city: Optional[str] = None,
        country: Optional[str] = None,
        business_type: Optional[str] = None,
        keywords: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        providers: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> PipelineResult:
        """Run a full search and return the pipeline result."""
        settings = self.context.settings
        query = SearchQuery(
            business_type=business_type,
            keywords=list(keywords or []),
            city=city or settings.default_city,
            country=country or settings.default_country,
            limit=limit or 50,
            # Left empty unless the caller asked for specific providers, so the
            # context's injected providers (or settings) are used instead.
            providers=list(providers) if providers else None,
        )
        return self.search(query, **kwargs)

    def search(self, query: SearchQuery, **kwargs: Any) -> PipelineResult:
        """Run the pipeline for an already-built query."""
        log.info(
            "Lead Finder starting: type=%s city=%s limit=%s",
            query.business_type,
            query.city,
            query.limit,
        )
        pipeline = LeadFinderPipeline(
            providers=self.context.resolve_providers(query.providers),
            checker=self.context.resolve_checker(),
            scorer=self.context.resolve_scorer(),
            repository=self.context.resolve_repository() if self.store_results else None,
            normalizer=self.context.normalizer
            or LeadNormalizer(
                default_country=query.country or self.context.settings.default_country,
                default_city=query.city or self.context.settings.default_city,
            ),
            deduplicator=self.context.deduplicator or Deduplicator(),
            check_websites=self.check_websites,
            store_results=self.store_results,
            max_checks=self.max_checks,
        )
        return pipeline.run(query)

    # -- reporting helpers -------------------------------------------------

    def list_leads(self, **kwargs: Any) -> List[Lead]:
        """Query stored leads (delegates to the repository)."""
        from lead_finder_agent.storage.base import LeadFilter

        repository = self.context.resolve_repository()
        return repository.find(LeadFilter(**{k: v for k, v in kwargs.items() if v is not None}))

    def top_leads(self, limit: int = 10) -> List[Lead]:
        """The highest scoring stored leads."""
        from lead_finder_agent.storage.base import LeadFilter

        repository = self.context.resolve_repository()
        return repository.find(LeadFilter(limit=limit))

    def export(self, path: str, fmt: Optional[str] = None, **filters: Any) -> str:
        """Export stored leads to JSON or CSV. Returns the written path."""
        leads = self.list_leads(**filters)
        target = export_leads(leads, path, fmt=fmt, metadata={"agent": self.name})
        return str(target)

    def stats(self) -> Dict[str, Any]:
        """Summary of what is stored."""
        repository = self.context.resolve_repository()
        stats_fn = getattr(repository, "stats", None)
        if callable(stats_fn):
            return stats_fn()
        return {"total": repository.count()}


__all__ = ["BaseAgent", "AgentContext", "LeadFinderAgent"]
