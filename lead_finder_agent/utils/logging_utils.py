"""Central logging configuration.

Logging is configured once, lazily, so importing library modules never mutates
global logging state on its own.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | None = None, force: bool = False) -> None:
    """Configure root logging for the library/CLI.

    Parameters
    ----------
    level:
        Log level name. Falls back to ``LEAD_FINDER_LOG_LEVEL`` then ``INFO``.
    force:
        Re-apply configuration even if it was already applied.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    resolved = (level or os.getenv("LEAD_FINDER_LOG_LEVEL") or "INFO").upper()
    numeric = getattr(logging, resolved, logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    root = logging.getLogger("lead_finder_agent")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, ensuring logging is configured."""
    setup_logging()
    if not name.startswith("lead_finder_agent"):
        name = f"lead_finder_agent.{name}"
    return logging.getLogger(name)


__all__ = ["get_logger", "setup_logging"]
