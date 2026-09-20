"""Dashboard / control centre for the Lead Finder system.

The dashboard is a **presentation and control layer**. It does not implement any
discovery, checking, scoring or analysis logic: every action it can take is
dispatched through the existing
:class:`~lead_finder_agent.core.manager.AgentManager`, and every read goes
through the existing repository and agent APIs.

Layering, from the outside in:

``static/``      a single-page UI; talks to ``/api`` over ``fetch``.
``server.py``    a stdlib threaded HTTP server for ``/api`` and the static files.
``api.py``       a small JSON router; validates input, scrubs error text.
``service.py``   the control layer; the only thing that reaches the Agent Manager.
``secrets.py``   central secret storage and masking.
``credentials.py`` one catalog of providers and the credential each one needs.
``agent_config.py`` per-agent instructions/settings/tools/enablement.
``jobs.py``      bounded run history and a background job runner.
``settings_store.py`` editable non-secret runtime settings overlay.

Import this module to get the service and server without pulling in the CLI::

    from lead_finder_agent.dashboard import DashboardService, create_server
"""

from __future__ import annotations

from lead_finder_agent.dashboard.agent_config import (
    AGENT_SETTINGS,
    AgentConfig,
    AgentConfigStore,
    KNOWN_TOOLS,
)
from lead_finder_agent.dashboard.credentials import (
    ProviderCredential,
    catalog as credential_catalog,
    secret_names,
)
from lead_finder_agent.dashboard.jobs import JobRecord, JobRunner, JobStore
from lead_finder_agent.dashboard.secrets import SecretStore, mask
from lead_finder_agent.dashboard.service import DashboardService
from lead_finder_agent.dashboard.settings_store import RuntimeSettingsStore

__all__ = [
    "DashboardService",
    "SecretStore",
    "SecretState",
    "AgentConfig",
    "AgentConfigStore",
    "AGENT_SETTINGS",
    "KNOWN_TOOLS",
    "JobRecord",
    "JobRunner",
    "JobStore",
    "ProviderCredential",
    "RuntimeSettingsStore",
    "create_server",
    "serve",
    "credential_catalog",
    "secret_names",
    "mask",
]


def __getattr__(name: str):
    """Import the server lazily.

    ``server`` imports ``http.server`` and the API layer, which pull in the
    agents. Keeping that out of the package import means
    ``from lead_finder_agent.dashboard import DashboardService`` stays cheap and
    does not configure logging or open a socket family unnecessarily.
    """
    if name in ("create_server", "serve", "DashboardServer"):
        from lead_finder_agent.dashboard import server as _server

        return getattr(_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ``create_server`` and ``serve`` are exported through ``__getattr__`` above;
# listing them in ``__all__`` keeps ``import *`` and documentation honest.