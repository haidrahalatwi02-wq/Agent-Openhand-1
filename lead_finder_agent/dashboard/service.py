"""Dashboard service: the control layer between the HTTP API and the agents.

Everything the dashboard can *do* goes through here, and everything here goes
through the existing :class:`~lead_finder_agent.core.manager.AgentManager`. No
business logic is reimplemented: discovery, checking, scoring, storage and
analysis all belong to the agents, and this module only decides which agent to
ask and packages the answer.

Two implementation notes that matter:

**Each run gets its own context.** :class:`AgentContext` owns a
:class:`~lead_finder_agent.storage.sqlite_repository.SQLiteLeadRepository`,
which holds a single sqlite connection. The dashboard serves requests from many
threads, so sharing one context between concurrent jobs would mean sharing one
connection across threads. A job therefore builds a private context, and reads
use a separate context behind a lock. This keeps thread-safety a property of the
dashboard rather than something sqlite has to tolerate.

**Disabling an agent is enforced here, not in a second registry.** The manager
remains the only registry; :meth:`DashboardService._check_enabled` is the single
gate that refuses to route work to a disabled agent.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from lead_finder_agent.config.settings import Settings, get_settings
from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent
from lead_finder_agent.core.manager import AgentManager
from lead_finder_agent.dashboard.agent_config import (
    AgentConfigStore,
    known_tools,
    settings_schema,
)
from lead_finder_agent.dashboard.credentials import catalog as credential_catalog
from lead_finder_agent.dashboard.credentials import resolve_secret_name
from lead_finder_agent.dashboard.jobs import (
    STATUS_COMPLETED,
    JobRunner,
    JobStore,
)
from lead_finder_agent.dashboard.secrets import SecretStore
from lead_finder_agent.dashboard.settings_store import RuntimeSettingsStore
from lead_finder_agent.models import WebsiteStatus
from lead_finder_agent.storage.base import LeadFilter
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.service")

#: Directory (under the data dir) holding dashboard state.
DASHBOARD_DIRNAME = "dashboard"

#: Score bands used by the overview. Kept here so the UI and the API agree.
SCORE_BANDS = (
    ("hot", 80, 100),
    ("warm", 60, 79),
    ("cold", 1, 59),
    ("unscored", 0, 0),
)

#: Hard cap on rows read for a dashboard listing. A dashboard is not a bulk
#: export tool; beyond this the API would be lying about "showing all".
MAX_SCAN = 5000


@dataclass
class ServicePaths:
    """Where the dashboard keeps its own state."""

    data_dir: Path

    @property
    def secrets(self) -> Path:
        return self.data_dir / "secrets.json"

    @property
    def agent_config(self) -> Path:
        return self.data_dir / "agent_config.json"

    @property
    def runs(self) -> Path:
        return self.data_dir / "runs.json"

    @property
    def settings(self) -> Path:
        return self.data_dir / "settings.json"


class DashboardService:
    """Read models and run control for the dashboard."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        data_dir: Optional[Path] = None,
        manager: Optional[AgentManager] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.data_dir = Path(data_dir) if data_dir else self._default_data_dir(self.settings)
        self.paths = ServicePaths(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.secrets = SecretStore(self.data_dir)
        self.agent_config = AgentConfigStore(self.data_dir)
        self.jobs = JobStore(self.data_dir)
        self.runner = JobRunner(self.jobs)
        self.runtime_settings = RuntimeSettingsStore(self.data_dir)

        # An externally supplied manager is respected, so a caller (or a test)
        # can inject one without the dashboard inventing its own registry.
        self._manager = manager
        self._shared_context: Optional[AgentContext] = None
        self._read_lock = threading.RLock()

    # -- wiring ------------------------------------------------------------

    @staticmethod
    def _default_data_dir(settings: Settings) -> Path:
        # Beside the database, so a project keeps all of its local state in the
        # directory it already gitignores.
        return Path(settings.db_path).parent / DASHBOARD_DIRNAME

    def effective_settings(self) -> Settings:
        """Base settings with the editable overlay applied."""
        return self.runtime_settings.apply(self.settings)

    def _build_context(self) -> AgentContext:
        """A fresh context with its own repository. Used per job."""
        settings = self.effective_settings()
        settings.ensure_directories()
        return AgentContext(settings=settings)

    def build_manager(self) -> AgentManager:
        """An :class:`AgentManager` with the project's default agents registered.

        This is the dashboard's only entry point to the agents. It is rebuilt per
        run so each run owns its context (and therefore its sqlite connection).

        Creating the repository writes the schema on first use. That is done
        while holding the read lock: a run and a read arriving together on a
        fresh database would otherwise race, and the loser failed with
        "database is locked". Serializing only the creation keeps the run itself
        concurrent with reads, which WAL supports.
        """
        context = self._build_context()
        with self._read_lock:
            context.resolve_repository()
        return AgentManager(context).register_default_agents()

    def manager(self) -> AgentManager:
        """The read-side manager, reused behind a lock.

        An externally injected manager wins, so the dashboard never becomes a
        second source of truth about which agents exist.
        """
        if self._manager is not None:
            return self._manager
        with self._read_lock:
            if self._manager is None:
                self._manager = AgentManager(self._read_context()).register_default_agents()
            return self._manager

    def _read_context(self) -> AgentContext:
        """The shared read-side context, created once behind a lock."""
        with self._read_lock:
            if self._shared_context is None:
                self._shared_context = self._build_context()
            return self._shared_context

    def close(self) -> None:
        if self._shared_context is not None:
            self._shared_context.close()
            self._shared_context = None

    # -- agents ------------------------------------------------------------

    def agent_names(self) -> List[str]:
        return self.manager().names()

    def is_enabled(self, name: str) -> bool:
        return self.agent_config.get(name).enabled

    def _check_enabled(self, name: str) -> None:
        """The single gate that refuses work for a disabled agent."""
        if not self.is_enabled(name):
            raise PermissionError(
                f"Agent {name!r} is disabled in the dashboard. Enable it to run it."
            )

    def list_agents(self) -> List[Dict[str, Any]]:
        """Every registered agent joined with its editable configuration.

        An agent that exists in code but has never been configured still appears,
        with defaults taken from the agent itself.
        """
        out: List[Dict[str, Any]] = []
        for agent in self.manager().agents():
            name = agent.name
            config = self.agent_config.get(name, agent=agent)
            info = dict(agent.info())
            out.append(
                {
                    **info,
                    "enabled": config.enabled,
                    "instructions": config.instructions
                    or str(getattr(agent, "description", "") or ""),
                    "settings": dict(config.settings),
                    "allowed_tools": list(config.allowed_tools),
                    # What the agent *can* use, so the UI can offer real choices.
                    "available_tools": known_tools(name),
                    "settings_schema": settings_schema(name),
                    "notes": config.notes,
                    # Informational only: agents never hold credentials.
                    "has_credentials": False,
                    "status": "enabled" if config.enabled else "disabled",
                }
            )
        return out

    def get_agent(self, name: str) -> Dict[str, Any]:
        for entry in self.list_agents():
            if entry["name"] == name:
                return entry
        raise KeyError(f"Unknown agent {name!r}")

    def update_agent(self, name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Update an agent's configuration. Rejects credential-shaped input.

        Refusing secret-looking keys here is a deliberate guard: the brief
        requires credentials to live in one central place, and silently
        accepting (then storing) an API key in an agent's settings would break
        that rule quietly. Failing loudly is the safer behaviour.
        """
        blocked = sorted(
            key
            for key in (payload.get("settings") or {})
            if _looks_secret(key)
        )
        if blocked:
            raise ValueError(
                "Credentials are managed centrally, not per agent. "
                f"Remove: {', '.join(blocked)}"
            )
        config = self.agent_config.update(
            name,
            enabled=payload.get("enabled"),
            instructions=payload.get("instructions"),
            settings=payload.get("settings"),
            allowed_tools=payload.get("allowed_tools"),
            notes=payload.get("notes"),
            agent=self._agent_or_none(name),
        )
        return {**self.get_agent(name), "enabled": config.enabled}

    def reset_agent(self, name: str) -> Dict[str, Any]:
        self.agent_config.reset(name, agent=self._agent_or_none(name))
        return self.get_agent(name)

    def _agent_or_none(self, name: str):
        try:
            return self.manager().get(name)
        except KeyError:
            return None

    # -- credentials -------------------------------------------------------

    def list_credentials(self) -> List[Dict[str, Any]]:
        """Every known provider with a masked credential state.

        Built from the catalog so a provider added later appears automatically.
        No secret value is ever included.
        """
        out: List[Dict[str, Any]] = []
        for entry in credential_catalog():
            data = entry.to_dict()
            if entry.secret_env:
                data["secret"] = self.secrets.describe(entry.secret_env).to_dict()
            else:
                # A provider that needs no key has no credential state. Reporting
                # it as "configured" would render a masked value that does not
                # exist, so say plainly that nothing is required.
                data["secret"] = {
                    "configured": False,
                    "source": None,
                    "preview": "",
                    "required": False,
                }
            out.append(data)
        return out

    def set_credential(self, name: str, value: str) -> Dict[str, Any]:
        """Store a secret centrally, then report only its masked state.

        The caller may name either a provider key (``google_places``) or the
        environment variable itself (``GOOGLE_PLACES_API_KEY``). Both resolve to
        the one canonical name declared in the catalog, which is the name the
        provider actually reads. Storing under the provider key instead would
        report success while the key never reached the provider, and the UI
        would then show the provider as unconfigured.
        """
        env_name = resolve_secret_name(name)
        return {"name": env_name, **self.secrets.set(env_name, value).to_dict()}

    def delete_credential(self, name: str) -> bool:
        return self.secrets.delete(resolve_secret_name(name))

    def redact(self, text: Any) -> str:
        return self.secrets.redact(text)

    # -- settings ----------------------------------------------------------

    def list_settings(self) -> Dict[str, Any]:
        return self.runtime_settings.effective()

    def update_settings(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        self.runtime_settings.update(values)
        return self.runtime_settings.effective()

    # -- leads -------------------------------------------------------------

    def _repository(self):
        return self._read_context().resolve_repository()

    def search_leads(
        self,
        *,
        city: Optional[str] = None,
        country: Optional[str] = None,
        business_type: Optional[str] = None,
        source: Optional[str] = None,
        priority: Optional[str] = None,
        website_status: Optional[str] = None,
        min_score: Optional[int] = None,
        max_score: Optional[int] = None,
        text: Optional[str] = None,
        order_by: str = "lead_score",
        descending: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Filtered, paginated lead listing.

        ``text`` matches the business name. The repository has no name filter,
        so it is applied here over the filtered rows; results are therefore
        capped at :data:`MAX_SCAN` and the response says when that cap was hit
        rather than implying the list is exhaustive.
        """
        filters = LeadFilter(
            city=city,
            country=country,
            business_type=business_type,
            source=source,
            priority=priority,
            website_status=WebsiteStatus(website_status) if website_status else None,
            min_score=min_score,
            max_score=max_score,
            order_by=order_by,
            descending=descending,
            limit=MAX_SCAN,
            offset=0,
        )
        with self._read_lock:
            rows = self._repository().find(filters)

        truncated = len(rows) >= MAX_SCAN
        if text:
            needle = str(text).strip().lower()
            rows = [r for r in rows if needle in (r.business_name or "").lower()]

        total = len(rows)
        page = rows[offset : offset + max(1, limit)]
        return {
            "leads": [lead_summary(lead) for lead in page],
            "total": total,
            "limit": max(1, limit),
            "offset": offset,
            "truncated": truncated,
        }

    def get_lead(self, lead_id: str) -> Dict[str, Any]:
        with self._read_lock:
            lead = self._repository().get(lead_id)
        if lead is None:
            raise KeyError(f"No lead with id {lead_id!r}")
        return lead_detail(lead)

    def lead_facets(self) -> Dict[str, Any]:
        """Distinct filter values, so the UI offers real choices."""
        with self._read_lock:
            rows = self._repository().find(LeadFilter(limit=MAX_SCAN))
        return {
            "cities": sorted({r.city for r in rows if r.city}),
            "countries": sorted({r.country for r in rows if r.country}),
            "business_types": sorted({r.business_type for r in rows if r.business_type}),
            "sources": sorted({s for r in rows for s in (r.sources or []) if s}),
            "priorities": ["hot", "warm", "cold", "disqualified"],
            "website_statuses": [str(s) for s in WebsiteStatus],
        }

    # -- overview ----------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """Counts and health for the landing page."""
        with self._read_lock:
            repository = self._repository()
            rows = repository.find(LeadFilter(limit=MAX_SCAN))
            total = repository.count()

        by_status: Dict[str, int] = {}
        by_priority: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        bands = {name: 0 for name, _, _ in SCORE_BANDS}
        scores = [int(r.lead_score or 0) for r in rows]

        for lead in rows:
            status = str(lead.website_status)
            by_status[status] = by_status.get(status, 0) + 1
            priority = str(lead.priority)
            by_priority[priority] = by_priority.get(priority, 0) + 1
            for src in lead.sources or ([lead.source] if lead.source else []):
                by_source[str(src)] = by_source.get(str(src), 0) + 1
            score = int(lead.lead_score or 0)
            for name, low, high in SCORE_BANDS:
                if low <= score <= high:
                    bands[name] += 1
                    break

        return {
            "leads": {
                "total": total,
                "scanned": len(rows),
                "by_website_status": by_status,
                "by_priority": by_priority,
                "by_source": by_source,
                "score_bands": bands,
                "average_score": round(sum(scores) / len(scores), 1) if scores else 0,
            },
            "agents": [
                {"name": a["name"], "status": a["status"], "enabled": a["enabled"]}
                for a in self.list_agents()
            ],
            "runs": self.jobs.counts(),
            "recent_runs": [r.to_dict() for r in self.jobs.recent(10)],
            "recent_errors": self.jobs.errors(10),
            "health": self.health(),
        }

    def health(self) -> Dict[str, Any]:
        """System health. Reports facts, never a credential."""
        checks: List[Dict[str, Any]] = []

        db_path = Path(self.effective_settings().db_path)
        db_ok = False
        db_detail = ""
        try:
            with self._read_lock:
                repository = self._repository()
                db_ok = repository.count() >= 0
            db_detail = str(db_path)
        except Exception as exc:  # noqa: BLE001 - health must not raise
            db_detail = self.redact(str(exc))

        checks.append(
            {
                "name": "database",
                "ok": db_ok,
                "detail": db_detail,
            }
        )

        data_ok = False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            data_ok = True
        except OSError as exc:
            db_detail = self.redact(str(exc))
        checks.append(
            {"name": "dashboard data directory", "ok": data_ok, "detail": str(self.data_dir)}
        )

        # Providers: availability is decided by the provider itself.
        available: List[str] = []
        unavailable: List[Dict[str, str]] = []
        try:
            from lead_finder_agent.search.registry import available_providers, registry

            for name in available_providers():
                try:
                    provider = registry().create(name)
                    reason = provider.unavailable_reason()
                except Exception as exc:  # noqa: BLE001
                    reason = self.redact(str(exc))
                if reason:
                    unavailable.append({"provider": name, "reason": reason})
                else:
                    available.append(name)
        except Exception as exc:  # noqa: BLE001
            unavailable.append({"provider": "registry", "reason": self.redact(str(exc))})

        checks.append(
            {
                "name": "search providers",
                "ok": bool(available),
                "detail": f"{len(available)} available",
            }
        )

        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "providers": {"available": available, "unavailable": unavailable},
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # -- runs --------------------------------------------------------------

    def start_search(
        self,
        *,
        city: Optional[str] = None,
        country: Optional[str] = None,
        business_type: Optional[str] = None,
        providers: Optional[List[str]] = None,
        limit: Optional[int] = None,
        check_websites: bool = True,
        max_checks: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Queue a Lead Finder run through the Agent Manager."""
        self._check_enabled(LeadFinderAgent.name)
        params: Dict[str, Any] = {
            "city": city,
            "country": country,
            "business_type": business_type,
            "providers": providers,
            "limit": limit,
            "check_websites": check_websites,
            "max_checks": max_checks,
        }

        def invoke() -> Dict[str, Any]:
            manager = self.build_manager()
            agent = manager.get(LeadFinderAgent.name)
            # Configure through the existing agent's own constructor parameters
            # rather than reaching into the pipeline.
            agent.check_websites = bool(check_websites)
            agent.max_checks = max_checks
            result = manager.run(
                LeadFinderAgent.name,
                city=city,
                country=country,
                business_type=business_type,
                providers=providers,
                limit=limit,
            )
            stats = result.stats.to_dict()
            return {
                "summary": f"{result.count} lead(s) found",
                "metrics": stats,
            }

        record = self.runner.submit(LeadFinderAgent.name, _clean(params), invoke)
        return record.to_dict()

    def start_analysis(
        self,
        *,
        limit: Optional[int] = None,
        lead_id: Optional[str] = None,
        store: bool = False,
        min_severity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue a Website Analyzer run through the Agent Manager."""
        from lead_finder_agent.agents import WebsiteAnalyzerAgent

        self._check_enabled(WebsiteAnalyzerAgent.name)
        params: Dict[str, Any] = {
            "limit": limit,
            "lead_id": lead_id,
            "store": store,
            "min_severity": min_severity,
        }

        def invoke() -> Dict[str, Any]:
            manager = self.build_manager()
            agent = manager.get(WebsiteAnalyzerAgent.name)
            agent.store = bool(store)
            analyses = manager.run(
                WebsiteAnalyzerAgent.name,
                limit=limit,
                lead_id=lead_id,
                min_severity=min_severity,
            )
            attention = sum(1 for a in analyses if a.needs_attention)
            return {
                "summary": f"{len(analyses)} lead(s) analysed, {attention} need attention",
                "metrics": {"analysed": len(analyses), "needs_attention": attention},
            }

        record = self.runner.submit(WebsiteAnalyzerAgent.name, _clean(params), invoke)
        return record.to_dict()

    def list_runs(self, limit: int = 25, status: Optional[str] = None) -> Dict[str, Any]:
        records = (
            self.jobs.by_status(status, limit) if status else self.jobs.recent(limit)
        )
        return {
            "runs": [r.to_dict() for r in records],
            "counts": self.jobs.counts(),
        }

    def get_run(self, job_id: str) -> Dict[str, Any]:
        record = self.jobs.get(job_id)
        if record is None:
            raise KeyError(f"No run with id {job_id!r}")
        return record.to_dict()


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #

#: Substrings that mark a settings key as credential-shaped. Used to reject
#: credential input on the per-agent endpoint.
_SECRET_HINTS = ("key", "secret", "token", "password", "passwd", "credential")


def _looks_secret(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return any(hint in text for hint in _SECRET_HINTS)


def _clean(params: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def lead_summary(lead: Any) -> Dict[str, Any]:
    """The fields the lead table shows. No internal payload."""
    return {
        "id": lead.id,
        "business_name": lead.business_name,
        "business_type": lead.business_type,
        "city": lead.city,
        "country": lead.country,
        "phone": lead.phone,
        "email": lead.email,
        "website_url": lead.website_url,
        "website_status": str(lead.website_status),
        "website_quality": str(lead.website_quality),
        "lead_score": int(lead.lead_score or 0),
        "score_confidence": str(lead.score_confidence),
        "priority": str(lead.priority),
        "sources": list(lead.sources or []),
        "source": lead.source,
        "business_status": str(lead.business_status),
        "discovered_at": _iso(lead.discovered_at),
    }


def lead_detail(lead: Any) -> Dict[str, Any]:
    """The full lead, including scoring reasons and any stored analysis."""
    data = lead_summary(lead)
    data.update(
        {
            "address": lead.address,
            "latitude": lead.latitude,
            "longitude": lead.longitude,
            "rating": lead.rating,
            "review_count": lead.review_count,
            "categories": list(lead.categories or []),
            "social_links": dict(lead.social_links or {}),
            "source_urls": dict(lead.source_urls or {}),
            "description": lead.description,
            "score_reason": list(lead.score_reason or []),
            "score_breakdown": dict(lead.score_breakdown or {}),
            "scoring_version": lead.scoring_version,
            "website_checked_at": _iso(lead.website_checked_at),
            "last_checked_at": _iso(lead.last_checked_at),
            "provider_ids": dict(lead.provider_ids or {}),
            # Produced by the Website Analyzer when it ran with store=True.
            "website_analysis": (lead.raw or {}).get("website_analysis"),
            "website_check": (lead.raw or {}).get("website_check"),
        }
    )
    return data


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = [
    "DashboardService",
    "ServicePaths",
    "lead_summary",
    "lead_detail",
    "SCORE_BANDS",
    "MAX_SCAN",
    "DASHBOARD_DIRNAME",
]