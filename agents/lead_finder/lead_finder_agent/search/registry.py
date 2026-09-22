"""Provider registry.

Adding a provider is a two-line change: implement
:class:`~lead_finder_agent.search.base.BaseSearchProvider` and decorate it with
:func:`register_provider`. Nothing else in the codebase needs to change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Type

from lead_finder_agent.search.base import BaseSearchProvider
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.registry")


class ProviderRegistry:
    """A name -> provider-class mapping."""

    def __init__(self) -> None:
        self._providers: Dict[str, Type[BaseSearchProvider]] = {}

    def register(self, cls: Type[BaseSearchProvider], name: Optional[str] = None) -> Type[BaseSearchProvider]:
        key = (name or getattr(cls, "name", None) or cls.__name__).lower()
        if not key or key == "base":
            raise ValueError(f"Cannot register provider with invalid name: {key!r}")
        cls.name = key
        self._providers[key] = cls
        log.debug("Registered search provider %r", key)
        return cls

    def get(self, name: str) -> Type[BaseSearchProvider]:
        key = str(name).lower()
        if key not in self._providers:
            raise KeyError(
                f"Unknown search provider {name!r}. Available: {sorted(self._providers)}"
            )
        return self._providers[key]

    def names(self) -> List[str]:
        return sorted(self._providers)

    def create(self, name: str, config: Optional[Mapping[str, Any]] = None) -> BaseSearchProvider:
        return self.get(name)(config=config)

    def __contains__(self, name: object) -> bool:
        return str(name).lower() in self._providers


_REGISTRY = ProviderRegistry()


def register_provider(
    cls: Optional[Type[BaseSearchProvider]] = None, name: Optional[str] = None
):
    """Class decorator that registers a provider.

    Usable both as ``@register_provider`` and
    ``@register_provider(name="my-provider")``.
    """

    def _wrap(target: Type[BaseSearchProvider]) -> Type[BaseSearchProvider]:
        _REGISTRY.register(target, name=name)
        return target

    if cls is not None:
        return _wrap(cls)
    return _wrap


def registry() -> ProviderRegistry:
    """The process-wide provider registry."""
    return _REGISTRY


def available_providers() -> List[str]:
    """Names of all registered providers."""
    _ensure_builtin_providers()
    return _REGISTRY.names()


def build_providers(
    names: Optional[List[str]] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> List[BaseSearchProvider]:
    """Instantiate the requested providers, skipping unknown names with a warning."""
    _ensure_builtin_providers()
    requested = names or ["osm", "sample"]
    providers: List[BaseSearchProvider] = []
    for name in requested:
        try:
            providers.append(_REGISTRY.create(name, config=config))
        except KeyError as exc:
            log.warning("%s", exc)
    return providers


_BUILTINS_LOADED = False


def _ensure_builtin_providers() -> None:
    """Import the built-in provider modules so they self-register."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from lead_finder_agent.search import providers  # noqa: F401,WPS433

    # Direct submodule imports guarantee registration even if the package
    # __init__ is customised later.
    from lead_finder_agent.search.providers import (  # noqa: F401,WPS433
        google_places,
        osm,
        sample,
    )


__all__ = [
    "ProviderRegistry",
    "registry",
    "register_provider",
    "available_providers",
    "build_providers",
]
