"""Registry for website checker strategies.

The pipeline asks for a checker by name instead of importing a concrete class, so
an additional verification strategy (a headless-browser probe, a paid uptime
service, a cached index of previously verified sites) can be added without the
core knowing about it. Registering a new strategy is a one-line change at the
edge; nothing in ``core`` or ``scoring`` needs to move.

The default is the HTTP checker, which needs no credentials and no paid service.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from lead_finder_agent.checker.base import BaseWebsiteChecker
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("checker.registry")

#: A builder takes an optional config mapping and returns a checker.
CheckerBuilder = Callable[[Optional[Dict[str, Any]]], BaseWebsiteChecker]

_REGISTRY: Dict[str, CheckerBuilder] = {}


def register_checker(name: str, builder: CheckerBuilder, *, replace: bool = False) -> None:
    """Register ``builder`` under ``name``.

    Refuses to silently overwrite an existing name unless ``replace`` is set, so
    an accidental duplicate registration cannot change behaviour quietly.
    """
    key = str(name).strip().lower()
    if not key:
        raise ValueError("Checker name must not be empty")
    if key in _REGISTRY and not replace:
        raise ValueError(f"A website checker named {key!r} is already registered")
    _REGISTRY[key] = builder
    log.debug("Registered website checker %r", key)


def available_checkers() -> list:
    """Names of every registered strategy, sorted for stable output."""
    return sorted(_REGISTRY)


def create_checker(
    name: str = "http", config: Optional[Dict[str, Any]] = None
) -> BaseWebsiteChecker:
    """Build the checker registered as ``name``."""
    key = str(name or "http").strip().lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown website checker {key!r}. Available: {', '.join(available_checkers())}"
        )
    return _REGISTRY[key](config)


def _build_http(config: Optional[Dict[str, Any]]) -> BaseWebsiteChecker:
    # Imported lazily so registering the checker does not pull in the HTTP stack
    # for callers that only need the registry.
    from lead_finder_agent.checker.http_checker import HttpWebsiteChecker

    return HttpWebsiteChecker(config=config)


register_checker("http", _build_http)


__all__ = ["register_checker", "available_checkers", "create_checker", "CheckerBuilder"]