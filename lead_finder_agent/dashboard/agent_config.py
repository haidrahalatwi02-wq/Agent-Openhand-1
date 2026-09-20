"""Per-agent configuration: instructions, settings, allowed tools, enablement.

This module deliberately does **not** reimplement agent management. The
:class:`~lead_finder_agent.core.manager.AgentManager` remains the registry and
orchestrator; this layer only holds the *editable configuration* for each agent
and a record of whether an agent is enabled. The dashboard reads the live agent
list from the manager and joins it with the configuration stored here, so an
agent that exists in code but has never been configured still appears.

Three rules keep this honest:

* **Enablement is enforced through the manager, not around it.** Disabling an
  agent records intent; :class:`~lead_finder_agent.dashboard.service.DashboardService`
  is what refuses to route work to a disabled agent. No second registry exists.
* **Credentials never live here.** An agent's configuration can name settings
  and tools; secret values are owned by
  :mod:`lead_finder_agent.dashboard.credentials` and
  :mod:`lead_finder_agent.dashboard.secrets`. This separation is what the brief
  asks for and it is what makes "no secret in agent config" testable.
* **Defaults come from the agent itself.** Instructions default to the agent's
  own ``description`` and settings default to the packaged config, so the UI
  shows real values instead of invented ones.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from lead_finder_agent.core.agent import BaseAgent
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.agent_config")

CONFIG_FILENAME = "agent_config.json"

#: Bumped when the on-disk shape changes, so an older file can be migrated
#: rather than silently misread.
CONFIG_VERSION = 1

#: Tools each shipped agent is actually able to use. These are the real
#: capabilities of the implementation, not a wish list: the Lead Finder runs the
#: discovery pipeline, the Website Analyzer reads stored checks.
KNOWN_TOOLS: Dict[str, List[str]] = {
    "lead_finder": [
        "search_providers",
        "website_checker",
        "lead_scoring",
        "storage",
        "export",
    ],
    "website_analyzer": [
        "storage",
        "website_checker",
    ],
}

#: Editable settings per agent, with the packaged default each falls back to.
#: Kept as plain data so the API can render a form without knowing the agent.
AGENT_SETTINGS: Dict[str, List[Dict[str, Any]]] = {
    "lead_finder": [
        {
            "name": "default_city",
            "label": "Default city",
            "type": "string",
            "help": "Used when a search does not name a city.",
        },
        {
            "name": "default_country",
            "label": "Default country",
            "type": "string",
            "help": "Used when a search does not name a country.",
        },
        {
            "name": "providers",
            "label": "Search providers",
            "type": "list",
            "help": "Provider names, in order. Unknown names are skipped with a warning.",
        },
        {
            "name": "max_checks",
            "label": "Maximum websites checked per run",
            "type": "integer",
            "help": "Caps how many sites one run fetches.",
        },
        {
            "name": "check_websites",
            "label": "Check websites",
            "type": "boolean",
            "help": "Turn off to discover and score without fetching any site.",
        },
    ],
    "website_analyzer": [
        {
            "name": "store",
            "label": "Save findings onto leads",
            "type": "boolean",
            "help": "Off by default: an analysis pass cannot alter stored leads unless asked.",
        },
        {
            "name": "limit",
            "label": "Default leads analysed per run",
            "type": "integer",
            "help": "Upper bound for a single analysis run.",
        },
        {
            "name": "min_severity",
            "label": "Minimum reported severity",
            "type": "string",
            "help": "One of info, low, medium, high.",
            "choices": ["info", "low", "medium", "high"],
        },
    ],
}


@dataclass
class AgentConfig:
    """Editable configuration for one agent. Contains no secrets."""

    name: str
    enabled: bool = True
    instructions: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)
    allowed_tools: List[str] = field(default_factory=list)
    #: Free-form note shown in the UI. Not used by any code path.
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "instructions": self.instructions,
            "settings": dict(self.settings),
            "allowed_tools": list(self.allowed_tools),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "AgentConfig":
        return cls(
            name=name,
            enabled=bool(data.get("enabled", True)),
            instructions=str(data.get("instructions") or ""),
            settings=dict(data.get("settings") or {}),
            allowed_tools=[str(t) for t in (data.get("allowed_tools") or [])],
            notes=str(data.get("notes") or ""),
        )


class AgentConfigStore:
    """Persists :class:`AgentConfig` objects as a single JSON file.

    Thread-safe and tolerant of a missing or corrupt file: an unreadable file
    yields defaults, because refusing to start the dashboard over a bad config
    file would be worse than starting with defaults.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / CONFIG_FILENAME
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, AgentConfig]] = None

    def _load(self) -> Dict[str, AgentConfig]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data: Dict[str, AgentConfig] = {}
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                    agents = raw.get("agents") if isinstance(raw, dict) else None
                    if isinstance(agents, dict):
                        for name, payload in agents.items():
                            if isinstance(payload, dict):
                                data[str(name)] = AgentConfig.from_dict(
                                    str(name), payload
                                )
            except (ValueError, OSError) as exc:
                log.warning("Could not read %s (%s); using defaults", self.path, exc)
                data = {}
            self._cache = data
            return data

    def _save(self, data: Dict[str, AgentConfig]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CONFIG_VERSION,
                "agents": {name: cfg.to_dict() for name, cfg in sorted(data.items())},
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self._cache = dict(data)

    # -- defaults ----------------------------------------------------------

    @staticmethod
    def default_for(agent: BaseAgent) -> AgentConfig:
        """Defaults derived from the agent itself."""
        name = str(getattr(agent, "name", "") or "").strip().lower()
        return AgentConfig(
            name=name,
            enabled=True,
            instructions=str(getattr(agent, "description", "") or ""),
            settings={},
            # An agent with no declared tools gets none, rather than a guess.
            allowed_tools=list(KNOWN_TOOLS.get(name, [])),
        )

    # -- reading -----------------------------------------------------------

    def get(self, name: str, agent: Optional[BaseAgent] = None) -> AgentConfig:
        """Configuration for ``name``, falling back to the agent's defaults."""
        key = str(name).strip().lower()
        stored = self._load().get(key)
        if stored is not None:
            return stored
        if agent is not None:
            return self.default_for(agent)
        return AgentConfig(name=key, instructions="")

    def all(self) -> Dict[str, AgentConfig]:
        return dict(self._load())

    # -- writing -----------------------------------------------------------

    def update(
        self,
        name: str,
        *,
        enabled: Optional[bool] = None,
        instructions: Optional[str] = None,
        settings: Optional[Mapping[str, Any]] = None,
        allowed_tools: Optional[Iterable[str]] = None,
        notes: Optional[str] = None,
        agent: Optional[BaseAgent] = None,
    ) -> AgentConfig:
        """Apply a partial update and return the stored configuration.

        Unknown settings keys are accepted rather than rejected: the store is
        agent-agnostic and an agent added later would otherwise need this file
        changed first. Values are coerced to JSON-safe primitives.
        """
        key = str(name).strip().lower()
        if not key:
            raise ValueError("An agent name is required")
        with self._lock:
            data = dict(self._load())
            current = data.get(key) or self.get(key, agent=agent)
            updated = AgentConfig(
                name=key,
                enabled=current.enabled if enabled is None else bool(enabled),
                instructions=(
                    current.instructions if instructions is None else str(instructions)
                ),
                settings=(
                    dict(current.settings)
                    if settings is None
                    else {str(k): _json_safe(v) for k, v in settings.items()}
                ),
                allowed_tools=(
                    list(current.allowed_tools)
                    if allowed_tools is None
                    else sorted({str(t) for t in allowed_tools})
                ),
                notes=current.notes if notes is None else str(notes),
            )
            data[key] = updated
            self._save(data)
            return updated

    def reset(self, name: str, agent: Optional[BaseAgent] = None) -> AgentConfig:
        """Drop stored overrides, returning the agent to its defaults."""
        key = str(name).strip().lower()
        with self._lock:
            data = dict(self._load())
            data.pop(key, None)
            self._save(data)
        return self.get(key, agent=agent)


def _json_safe(value: Any) -> Any:
    """Coerce a value to something JSON can hold, without inventing types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def settings_schema(name: str) -> List[Dict[str, Any]]:
    """Editable settings declared for ``name`` (empty for an unknown agent)."""
    return list(AGENT_SETTINGS.get(str(name).strip().lower(), []))


def known_tools(name: str) -> List[str]:
    return list(KNOWN_TOOLS.get(str(name).strip().lower(), []))


__all__ = [
    "AgentConfig",
    "AgentConfigStore",
    "AGENT_SETTINGS",
    "KNOWN_TOOLS",
    "CONFIG_FILENAME",
    "settings_schema",
    "known_tools",
]