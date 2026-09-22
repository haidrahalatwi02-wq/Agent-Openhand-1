"""Website checker interface."""

from __future__ import annotations

import abc
from typing import List, Optional, Sequence

from lead_finder_agent.models import Lead, WebsiteCheckResult, WebsiteStatus
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("checker.base")


class BaseWebsiteChecker(abc.ABC):
    """Decides whether a lead has a website, and how good it is.

    Implementations must honour one rule above all others: **an inconclusive
    check is never reported as "no website"**. Use
    :attr:`~lead_finder_agent.models.WebsiteStatus.UNKNOWN` for those cases, so
    scoring can weight them conservatively.
    """

    name: str = "base"

    @abc.abstractmethod
    def check(self, lead: Lead) -> WebsiteCheckResult:
        """Inspect a single lead."""

    def check_many(
        self, leads: Sequence[Lead], limit: Optional[int] = None
    ) -> List[WebsiteCheckResult]:
        """Inspect several leads, stopping after ``limit`` checks."""
        results: List[WebsiteCheckResult] = []
        for index, lead in enumerate(leads):
            if limit is not None and index >= limit:
                break
            try:
                results.append(self.check(lead))
            except Exception as exc:  # noqa: BLE001 - a bad lead must not stop the run
                log.warning("Website check failed for %r: %s", lead.business_name, exc)
                results.append(
                    WebsiteCheckResult(
                        status=WebsiteStatus.UNKNOWN,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = ["BaseWebsiteChecker"]
