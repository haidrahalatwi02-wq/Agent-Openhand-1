"""Base class for search providers.

A provider is responsible for exactly one thing: given a :class:`SearchQuery`,
return a list of raw dictionaries. Normalization, de-duplication, website
checking and scoring all happen later in the pipeline. Keeping that boundary
strict is what makes providers replaceable.
"""

from __future__ import annotations

import abc
import time
from typing import Any, Dict, List, Mapping, Optional

from lead_finder_agent.models import ProviderKind, ProviderResponse, SearchQuery
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.base")


class ProviderSkip(Exception):
    """Raised by a provider that cannot run (missing key, disabled, ...).

    A skip is not an error: the pipeline records the reason and moves on.
    """


class BaseSearchProvider(abc.ABC):
    """Interface every search provider implements."""

    name: str = "base"
    kind: ProviderKind = ProviderKind.API
    # Setting this to an env var name makes the provider check for a key and
    # skip itself when the key is absent, instead of failing.
    requires_key_env: Optional[str] = None
    description: str = ""

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = dict(config or {})

    # -- to implement ------------------------------------------------------

    @abc.abstractmethod
    def search_raw(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """Return raw provider records for ``query`` (no normalization)."""

    # -- plumbing ----------------------------------------------------------

    def is_available(self) -> bool:
        """Whether this provider can run in the current environment."""
        if not self.requires_key_env:
            return True
        import os

        return bool(os.getenv(self.requires_key_env))

    def unavailable_reason(self) -> Optional[str]:
        if self.is_available():
            return None
        return f"{self.name} requires the {self.requires_key_env} environment variable"

    def search(self, query: SearchQuery) -> ProviderResponse:
        """Run the provider, converting any failure into a response object.

        Providers must never raise out of this method: one broken source should
        not stop the rest of the pipeline.
        """
        started = time.perf_counter()
        if not self.is_available():
            return ProviderResponse(
                provider=self.name,
                kind=self.kind,
                skipped_reason=self.unavailable_reason(),
                elapsed_seconds=time.perf_counter() - started,
            )

        try:
            leads = self.search_raw(query) or []
        except ProviderSkip as exc:
            log.info("Provider %s skipped: %s", self.name, exc)
            return ProviderResponse(
                provider=self.name,
                kind=self.kind,
                skipped_reason=str(exc),
                elapsed_seconds=time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate providers
            log.warning("Provider %s failed: %s", self.name, exc)
            return ProviderResponse(
                provider=self.name,
                kind=self.kind,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=time.perf_counter() - started,
            )

        return ProviderResponse(
            provider=self.name,
            kind=self.kind,
            leads=list(leads),
            elapsed_seconds=time.perf_counter() - started,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = ["BaseSearchProvider", "ProviderSkip"]
