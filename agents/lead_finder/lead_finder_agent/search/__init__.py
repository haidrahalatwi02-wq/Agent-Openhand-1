"""Search providers and the registry that resolves them by name."""

from lead_finder_agent.search.base import (
    BaseSearchProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderSkip,
)
from lead_finder_agent.search.registry import (
    ProviderRegistry,
    available_providers,
    build_providers,
    register_provider,
)
from lead_finder_agent.search.multi import MultiProviderSearch

__all__ = [
    "BaseSearchProvider",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderSkip",
    "ProviderRegistry",
    "register_provider",
    "available_providers",
    "build_providers",
    "MultiProviderSearch",
]
