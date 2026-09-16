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
from lead_finder_agent.models.enums import StrEnum
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("search.base")


class ProviderErrorKind(StrEnum):
    """Why a provider could not deliver results.

    These are kept distinct because they call for different reactions: a missing
    key is the operator's to fix, a rate limit is worth retrying later, and "no
    results" is a perfectly successful run. Collapsing them into one generic
    error would hide that difference from the CLI user and the logs.
    """

    #: The provider is not configured correctly (bad endpoint, bad params).
    INVALID_CONFIG = "invalid_config"
    #: A required credential is absent.
    MISSING_KEY = "missing_key"
    #: The source asked us to slow down (HTTP 429 / quota message).
    RATE_LIMITED = "rate_limited"
    #: A transport problem: DNS, TLS, timeout, connection reset.
    NETWORK = "network"
    #: The source answered, but not in a shape we can use.
    MALFORMED_RESPONSE = "malformed_response"
    #: Anything that does not fit the cases above.
    PROVIDER_ERROR = "provider_error"


class ProviderError(Exception):
    """Raised by a provider that cannot complete a search.

    Carries a machine-readable :class:`ProviderErrorKind` so the caller can
    react to the *kind* of failure rather than string-matching a message.

    Never include credentials in the message: it is echoed to the user and
    written to logs.
    """

    def __init__(
        self,
        message: str,
        kind: ProviderErrorKind = ProviderErrorKind.PROVIDER_ERROR,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        # Rate limits and network hiccups are transient; bad config is not.
        if retryable is None:
            retryable = kind in (ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.NETWORK)
        self.retryable = retryable


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
        #: Number of remote pages the last :meth:`search` call consumed.
        self._pages_fetched: int = 0

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

        self._pages_fetched = 0
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
        except ProviderError as exc:
            # A classified failure: keep the kind so callers can tell a rate
            # limit from a bad key without parsing the message.
            log.warning("Provider %s failed (%s): %s", self.name, exc.kind, exc)
            return ProviderResponse(
                provider=self.name,
                kind=self.kind,
                error=f"{exc.kind}: {exc}",
                error_kind=str(exc.kind),
                elapsed_seconds=time.perf_counter() - started,
                pages_fetched=self._pages_fetched,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate providers
            log.warning("Provider %s failed: %s", self.name, exc)
            return ProviderResponse(
                provider=self.name,
                kind=self.kind,
                error=f"{type(exc).__name__}: {exc}",
                error_kind=str(ProviderErrorKind.PROVIDER_ERROR),
                elapsed_seconds=time.perf_counter() - started,
                pages_fetched=self._pages_fetched,
            )

        return ProviderResponse(
            provider=self.name,
            kind=self.kind,
            leads=list(leads),
            elapsed_seconds=time.perf_counter() - started,
            pages_fetched=self._pages_fetched,
        )

    # -- paging bookkeeping ------------------------------------------------

    def _note_page(self) -> None:
        """Record that one more remote page was fetched.

        Providers that page through an API should call this, so the caller can
        report how many requests a search actually cost.
        """
        self._pages_fetched += 1

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = [
    "BaseSearchProvider",
    "ProviderSkip",
    "ProviderError",
    "ProviderErrorKind",
]
