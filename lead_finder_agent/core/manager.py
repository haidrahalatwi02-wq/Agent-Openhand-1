"""Agent Manager: registers agents by name and routes work between them.

This is the coordination layer the rest of the project was built towards. The
Lead Finder is the first agent; the manager is what lets a second one exist
without the pipeline, the CLI or the Lead Finder itself changing.

The manager deliberately does very little:

* :meth:`AgentManager.register` adds an agent under its ``name``.
* :meth:`AgentManager.run` invokes one agent and hands back its result.
* :meth:`AgentManager.run_all` fans out to several agents **with per-agent
  isolation**, the same way :class:`~lead_finder_agent.search.multi.MultiProviderSearch`
  isolates providers: one agent failing must not discard the work the others
  already did.

Two design choices worth stating, because they are easy to get wrong:

**The registry refuses silent replacement.** Registering a second agent under an
existing name raises unless ``replace=True`` is passed. A manager that quietly
swapped an agent would change behaviour invisibly, and the failure would surface
far from the cause. This matches the checker registry.

**Agents are shared, not rebuilt per run.** Every agent registered against one
manager receives the *same* :class:`~lead_finder_agent.core.agent.AgentContext`,
so a search and a follow-up read and write one repository and one settings
object. That is the point of the context: coordinating through shared state
rather than agents importing each other.

Nothing here imports a concrete agent beyond the Lead Finder, and the Lead Finder
does not know the manager exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from lead_finder_agent.core.agent import AgentContext, BaseAgent, LeadFinderAgent
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("core.manager")


@dataclass
class AgentRunResult:
    """Outcome of running one agent through the manager.

    ``error`` is populated instead of raising when the manager is asked to run
    agents in isolation, so a caller can report partial success honestly rather
    than pretending every agent worked.
    """

    agent: str
    ok: bool
    result: Any = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "ok": self.ok,
            "error": self.error,
        }


class AgentManager:
    """A registry of agents that share one :class:`AgentContext`.

    Typical use::

        manager = AgentManager()
        manager.register(LeadFinderAgent(manager.context))
        manager.get("lead_finder").run(city="Aden", limit=20)

    Adding another agent is a one-line change at the edge: subclass
    :class:`BaseAgent` and register it. Nothing in ``core.pipeline`` moves.
    """

    def __init__(self, context: Optional[AgentContext] = None) -> None:
        self.context = context or AgentContext()
        self._agents: Dict[str, BaseAgent] = {}

    # -- registration ------------------------------------------------------

    def register(self, agent: BaseAgent, *, replace: bool = False) -> BaseAgent:
        """Register ``agent`` under its ``name`` and return it.

        Raises ``ValueError`` on a duplicate name unless ``replace`` is set, so
        an accident cannot silently change which agent answers a name.
        """
        if not isinstance(agent, BaseAgent):
            raise TypeError(f"Expected a BaseAgent, got {type(agent).__name__}")
        name = str(getattr(agent, "name", "") or "").strip().lower()
        if not name:
            raise ValueError("Agent must define a non-empty 'name'")
        if name in self._agents and not replace:
            raise ValueError(f"An agent named {name!r} is already registered")
        self._agents[name] = agent
        log.debug("Registered agent %r", name)
        return agent

    def unregister(self, name: str) -> Optional[BaseAgent]:
        """Remove an agent, returning it if it was registered."""
        return self._agents.pop(str(name).strip().lower(), None)

    def get(self, name: str) -> BaseAgent:
        """Return the agent registered as ``name``."""
        key = str(name).strip().lower()
        if key not in self._agents:
            raise KeyError(
                f"Unknown agent {name!r}. Available: {', '.join(self.names()) or '(none)'}"
            )
        return self._agents[key]

    def names(self) -> List[str]:
        """Registered agent names, sorted for stable output."""
        return sorted(self._agents)

    def agents(self) -> List[BaseAgent]:
        """Registered agents in name order."""
        return [self._agents[name] for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return str(name).strip().lower() in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self) -> Iterator[BaseAgent]:
        return iter(self.agents())

    # -- running -----------------------------------------------------------

    def run(self, name: str, **kwargs: Any) -> Any:
        """Run the named agent with ``kwargs`` and return its result.

        Failures propagate. Use :meth:`run_isolated` or :meth:`run_all` when a
        partial result is more useful than an exception.
        """
        agent = self.get(name)
        log.info("Manager running agent %r", agent.name)
        return agent.run(**kwargs)

    def run_isolated(self, name: str, **kwargs: Any) -> AgentRunResult:
        """Run one agent, converting a failure into an :class:`AgentRunResult`.

        This is the seam that keeps one broken agent from taking down a
        multi-agent run.
        """
        try:
            return AgentRunResult(agent=name, ok=True, result=self.run(name, **kwargs))
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            log.warning("Agent %r failed: %s", name, exc)
            return AgentRunResult(agent=name, ok=False, error=str(exc))

    def run_all(
        self,
        names: Optional[Sequence[str]] = None,
        *,
        kwargs_by_agent: Optional[Mapping[str, Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> List[AgentRunResult]:
        """Run several agents in isolation and return one result each.

        ``names`` defaults to every registered agent. ``kwargs_by_agent`` lets a
        caller give one agent different arguments while ``kwargs`` applies to
        the rest; without that, fanning out a single keyword set to agents with
        different signatures would fail for the wrong reason.
        """
        targets = list(names) if names is not None else self.names()
        per_agent = dict(kwargs_by_agent or {})
        results: List[AgentRunResult] = []
        for name in targets:
            call_kwargs = {**kwargs, **per_agent.get(str(name), {})}
            results.append(self.run_isolated(name, **call_kwargs))
        return results

    def register_default_agents(self) -> "AgentManager":
        """Register the agents that ship with the project.

        The Lead Finder owns discovery; the Website Analyzer reads what it
        stored. This exists so a caller gets the standard set without knowing
        the class names, and so adding the next built-in agent is a single line
        here rather than a change in every caller.

        The analyzer is imported lazily: ``agents`` depends on ``core.agent``,
        and importing it at module scope would make the package's import order
        matter for no benefit.
        """
        if LeadFinderAgent.name not in self._agents:
            self.register(LeadFinderAgent(self.context))

        from lead_finder_agent.agents.website_analyzer import WebsiteAnalyzerAgent

        if WebsiteAnalyzerAgent.name not in self._agents:
            self.register(WebsiteAnalyzerAgent(self.context))
        return self

    def info(self) -> List[Dict[str, str]]:
        """Describe every registered agent, for `--json` output and the CLI."""
        return [agent.info() for agent in self.agents()]


__all__ = ["AgentManager", "AgentRunResult"]