"""Website Analyzer Agent: the second agent on top of the Agent Manager.

The Lead Finder answers "does this business have a website?" with one status.
That is enough to rank a lead, but not enough to *talk* to one: an outreach
message needs to know *why* a site is weak, not only that it is. This agent
reads the leads the Lead Finder already stored and turns each stored website
check into a short, evidence-backed analysis.

Three rules shape the design.

**It never re-derives a website status.** The status comes from the stored
:class:`~lead_finder_agent.models.WebsiteCheckResult` the checker produced.
Re-running a check here would let the analyzer contradict the lead it is
describing, and would put network access on a read-only reporting path. The
checker is only used when a caller explicitly passes ``recheck=True`` for a lead
that has no usable stored check.

**It inherits the honesty invariant.** ``website_unknown`` means "we could not
tell". That is reported as a finding about *our* knowledge, never as evidence
about the business, and a lead whose site was never verified is never described
as having no website. The same reasoning keeps such a lead out of the ``hot``
priority in scoring.

**Absent data is not a negative finding.** A stored check that omits a field
(an older record, a hand-written payload) must not produce a confident claim
about it. Findings that depend on a detail are only emitted when that detail was
actually recorded.

The agent is read-only by default: it annotates the analyses it returns and does
not write to the repository unless ``store=True`` is passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from lead_finder_agent.config.loader import load_packaged_data
from lead_finder_agent.core.agent import AgentContext, BaseAgent
from lead_finder_agent.models import (
    Lead,
    WebsiteCheckResult,
    WebsiteErrorKind,
    WebsiteIdentity,
    WebsiteQuality,
    WebsiteStatus,
)
from lead_finder_agent.scoring.engine import hydrate_website_check
from lead_finder_agent.storage.base import LeadFilter
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("agents.website_analyzer")

#: Severity ordering, so findings can be ranked without repeating the order at
#: every call site. ``info`` sits below ``low``: it is context, not a problem.
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}

#: Findings at or above this level mean the lead is worth acting on.
ATTENTION_SEVERITIES = frozenset({"medium", "high"})

#: Statuses that describe our own knowledge rather than the business. A finding
#: derived from one of these must never be worded as a statement about the site.
INCONCLUSIVE_STATUSES = frozenset({WebsiteStatus.UNKNOWN, WebsiteStatus.NOT_CHECKED})


@dataclass
class WebsiteFinding:
    """One observation about a lead's web presence.

    ``severity`` is a label, not a score. This agent deliberately does not feed
    the lead score: re-scoring a stored lead from an analysis pass would make a
    stored score depend on whether, and when, the analyzer last ran.
    """

    kind: str
    severity: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "severity": self.severity, "detail": self.detail}


@dataclass
class WebsiteAnalysis:
    """The analyzer's verdict on one lead."""

    lead_id: Optional[str]
    business_name: str
    status: WebsiteStatus
    quality: WebsiteQuality
    identity: WebsiteIdentity = WebsiteIdentity.NOT_APPLICABLE
    findings: List[WebsiteFinding] = field(default_factory=list)
    #: True only when the evidence confirms something worth acting on. A lead
    #: whose check was inconclusive is not "fine" either — read the findings.
    needs_attention: bool = False
    #: Whether a stored check was available. Distinguishes "analysed from
    #: recorded evidence" from "analysed from the lead's own fields".
    from_stored_check: bool = False

    def finding_kinds(self) -> List[str]:
        return [f.kind for f in self.findings]

    def severity_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "business_name": self.business_name,
            "status": str(self.status),
            "quality": str(self.quality),
            "identity": str(self.identity),
            "from_stored_check": self.from_stored_check,
            "needs_attention": self.needs_attention,
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary(self) -> str:
        """One line suitable for a table or a log."""
        kinds = ", ".join(self.finding_kinds())
        return f"{self.business_name}: {kinds}" if kinds else f"{self.business_name}: no findings"


def load_analysis_config(override: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Load the packaged analysis thresholds and severities.

    ``override`` is merged one level deep, so a caller can replace just
    ``{"thresholds": {...}}`` without having to restate the severities.
    """
    data = dict(load_packaged_data("website_analysis"))
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(data.get(key), Mapping):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return data


class WebsiteAnalyzerAgent(BaseAgent):
    """Analyse the websites of stored leads and report what is wrong with them.

    Parameters:
        context: the shared :class:`AgentContext`; supplies the repository, the
            checker and the settings.
        config: overrides for ``website_analysis.yaml`` (thresholds/severities).
        store: write findings back onto the stored leads. Off by default, so an
            analysis run cannot alter stored leads unless it was asked to.
    """

    name = "website_analyzer"
    description = "Analyse the quality and identity of a lead's website"

    def __init__(
        self,
        context: Optional[AgentContext] = None,
        config: Optional[Mapping[str, Any]] = None,
        store: bool = False,
    ) -> None:
        super().__init__(context)
        self.config = load_analysis_config(config)
        self.store = store

    # -- configuration accessors ------------------------------------------

    @property
    def thresholds(self) -> Dict[str, Any]:
        return dict(self.config.get("thresholds") or {})

    @property
    def severities(self) -> Dict[str, Any]:
        return dict(self.config.get("severities") or {})

    def _severity(self, kind: str) -> str:
        return str(self.severities.get(kind) or "low")

    def _threshold(self, name: str, default: Any) -> Any:
        value = self.thresholds.get(name)
        return default if value is None else value

    # -- entry point -------------------------------------------------------

    def run(
        self,
        limit: Optional[int] = None,
        lead_id: Optional[str] = None,
        statuses: Optional[Sequence[WebsiteStatus]] = None,
        recheck: bool = False,
        min_severity: Optional[str] = None,
        **kwargs: Any,
    ) -> List[WebsiteAnalysis]:
        """Analyse stored leads and return one analysis each.

        ``statuses`` narrows which website statuses are considered; the default
        is every stored lead, because a lead with no website is exactly the one
        worth analysing. ``recheck`` lets the checker fill in a missing check for
        a lead that has none, and is the only path here that touches the network.
        """
        if limit is not None and limit < 0:
            raise ValueError("limit must not be negative")

        floor = None
        if min_severity is not None:
            floor = SEVERITY_ORDER.get(str(min_severity).lower())
            if floor is None:
                raise ValueError(
                    f"Unknown severity {min_severity!r}. "
                    f"Available: {', '.join(sorted(SEVERITY_ORDER))}"
                )

        leads = self._select_leads(limit=limit, lead_id=lead_id, statuses=statuses)
        analyses = [self.analyse(lead, recheck=recheck) for lead in leads]

        if floor is not None:
            analyses = [
                a
                for a in analyses
                if any(SEVERITY_ORDER.get(f.severity, 0) >= floor for f in a.findings)
            ]

        if self.store:
            self._persist(analyses)

        log.info("Website analyzer finished: %s lead(s) analysed", len(analyses))
        return analyses

    def _select_leads(
        self,
        limit: Optional[int],
        lead_id: Optional[str],
        statuses: Optional[Sequence[WebsiteStatus]],
    ) -> List[Lead]:
        repository = self.context.resolve_repository()

        if lead_id:
            found = repository.get(lead_id)
            return [found] if found is not None else []

        wanted = {WebsiteStatus(s) for s in statuses} if statuses else None
        if wanted is None:
            return repository.find(LeadFilter(limit=limit))

        # LeadFilter takes a single status, and filtering *after* applying the
        # limit would return fewer leads than asked for whenever some were
        # filtered out, so the limit is applied here instead.
        leads = repository.find(LeadFilter())
        selected = [lead for lead in leads if lead.website_status in wanted]
        return selected[:limit] if limit is not None else selected

    # -- analysis ----------------------------------------------------------

    def analyse(self, lead: Lead, recheck: bool = False) -> WebsiteAnalysis:
        """Analyse one lead, reusing its stored check when there is one."""
        check = hydrate_website_check(lead)
        payload = self._stored_payload(lead)

        if check is None and recheck:
            check = self._recheck(lead)
            payload = check.to_dict() if check is not None else {}

        # Without a stored check, fall back to the lead's own fields — the same
        # fallback the scorer uses. The status is never invented here.
        status = check.status if check is not None else lead.website_status
        quality = check.quality if check is not None else lead.website_quality
        identity = (
            getattr(check, "identity", WebsiteIdentity.NOT_APPLICABLE)
            if check is not None
            else WebsiteIdentity.NOT_APPLICABLE
        )

        findings = self._findings(
            lead=lead,
            check=check,
            payload=payload,
            status=status,
            quality=quality,
            identity=identity,
        )
        return WebsiteAnalysis(
            lead_id=lead.id,
            business_name=lead.business_name,
            status=status,
            quality=quality,
            identity=identity,
            findings=findings,
            needs_attention=any(f.severity in ATTENTION_SEVERITIES for f in findings),
            from_stored_check=check is not None,
        )

    @staticmethod
    def _stored_payload(lead: Lead) -> Dict[str, Any]:
        raw = getattr(lead, "raw", None)
        if not isinstance(raw, Mapping):
            return {}
        payload = raw.get("website_check")
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _recheck(self, lead: Lead) -> Optional[WebsiteCheckResult]:
        """Ask the checker about a lead that has no usable stored check."""
        checker = self.context.resolve_checker()
        try:
            return checker.check(lead)
        except Exception as exc:  # noqa: BLE001 - analysis must not abort a run
            log.warning("Re-check failed for %r: %s", lead.business_name, exc)
            return None

    def _findings(
        self,
        lead: Lead,
        check: Optional[WebsiteCheckResult],
        payload: Mapping[str, Any],
        status: WebsiteStatus,
        quality: WebsiteQuality,
        identity: WebsiteIdentity,
    ) -> List[WebsiteFinding]:
        findings: List[WebsiteFinding] = []

        if status == WebsiteStatus.NOT_FOUND:
            findings.append(
                WebsiteFinding(
                    "no_website_confirmed",
                    self._severity("no_website_confirmed"),
                    "The server confirmed the page does not exist, so this "
                    "business has no working website.",
                )
            )
        elif status == WebsiteStatus.UNREACHABLE:
            findings.append(
                WebsiteFinding(
                    "site_unreachable",
                    self._severity("site_unreachable"),
                    "A website URL exists but the request failed, so the site is "
                    "not reliably reachable.",
                )
            )
        elif status in INCONCLUSIVE_STATUSES:
            # Worded as a gap in our knowledge, never as a fact about the site.
            findings.append(
                WebsiteFinding(
                    "check_unavailable",
                    self._severity("check_unavailable"),
                    "No website was confirmed. That means the check was "
                    "inconclusive, not that the business lacks a website.",
                )
            )

        if quality == WebsiteQuality.SOCIAL_ONLY:
            findings.append(
                WebsiteFinding(
                    "social_only_presence",
                    self._severity("social_only_presence"),
                    "The only web presence is a social media profile, which the "
                    "business does not own or control.",
                )
            )
        elif quality == WebsiteQuality.WEAK and status == WebsiteStatus.EXISTS:
            findings.append(
                WebsiteFinding(
                    "weak_quality",
                    self._severity("weak_quality"),
                    "The site is reachable but thin, outdated, or hosted on a "
                    "free website builder.",
                )
            )

        if check is not None:
            findings.extend(self._check_findings(check, payload, quality, identity))
        elif status == WebsiteStatus.EXISTS:
            # No stored detail, so the only thing we can honestly say about the
            # scheme is what the lead's own URL field shows.
            url = (lead.website_url or "").strip().lower()
            if url.startswith("http://"):
                findings.append(
                    WebsiteFinding(
                        "no_https",
                        self._severity("no_https"),
                        "The site is served without HTTPS.",
                    )
                )

        return findings

    def _check_findings(
        self,
        check: WebsiteCheckResult,
        payload: Mapping[str, Any],
        quality: WebsiteQuality,
        identity: WebsiteIdentity,
    ) -> List[WebsiteFinding]:
        """Findings that need the detail recorded by the checker.

        A detail is only reported when the stored payload actually carries it,
        so a record that omits a field is treated as unknown rather than as a
        recorded negative.
        """
        findings: List[WebsiteFinding] = []

        if check.error_kind == WebsiteErrorKind.INVALID_URL:
            findings.append(
                WebsiteFinding(
                    "check_unavailable",
                    self._severity("check_unavailable"),
                    "The website value supplied by the source is not a usable web "
                    "address.",
                )
            )

        # The detail findings below describe a real site. A social profile is
        # not one, so reporting "no contact page" or "no shop" against it would
        # be noise about a page that was never meant to have either.
        if check.status != WebsiteStatus.EXISTS or quality == WebsiteQuality.SOCIAL_ONLY:
            return findings

        if self._recorded(payload, "has_https") and not check.has_https:
            findings.append(
                WebsiteFinding(
                    "no_https",
                    self._severity("no_https"),
                    "The site is served without HTTPS.",
                )
            )
        if self._recorded(payload, "has_contact_page") and not check.has_contact_page:
            findings.append(
                WebsiteFinding(
                    "no_contact_details",
                    self._severity("no_contact_details"),
                    "No contact details or contact page were found.",
                )
            )
        if self._recorded(payload, "has_shop") and not check.has_shop:
            findings.append(
                WebsiteFinding(
                    "no_shop",
                    self._severity("no_shop"),
                    "No e-commerce or ordering markers were found.",
                )
            )
        if identity == WebsiteIdentity.UNCERTAIN:
            findings.append(
                WebsiteFinding(
                    "identity_uncertain",
                    self._severity("identity_uncertain"),
                    "The page was reachable but could not be confidently tied to "
                    "this business.",
                )
            )

        slow_after = self._threshold("slow_response_ms", 3000)
        if (
            self._recorded(payload, "response_time_ms")
            and check.response_time_ms is not None
            and check.response_time_ms > slow_after
        ):
            findings.append(
                WebsiteFinding(
                    "slow_response",
                    self._severity("slow_response"),
                    f"The site took {check.response_time_ms} ms to respond, over "
                    f"the {slow_after} ms threshold.",
                )
            )
        if self._recorded(payload, "truncated") and check.truncated:
            findings.append(
                WebsiteFinding(
                    "truncated_response",
                    self._severity("truncated_response"),
                    "The page was cut off at the configured size limit, so this "
                    "analysis is incomplete.",
                )
            )

        min_title = int(self._threshold("min_title_length", 3))
        title = (check.page_title or "").strip()
        if self._recorded(payload, "page_title") and len(title) < min_title:
            findings.append(
                WebsiteFinding(
                    "missing_title",
                    self._severity("missing_title"),
                    "The page has no usable title.",
                )
            )

        return findings

    @staticmethod
    def _recorded(payload: Mapping[str, Any], key: str) -> bool:
        """Whether the stored check actually recorded ``key``."""
        return key in payload and payload[key] is not None

    # -- persistence -------------------------------------------------------

    def _persist(self, analyses: Sequence[WebsiteAnalysis]) -> int:
        """Store findings on the leads' raw payloads.

        Kept off the score: this is descriptive detail, and writing it into the
        score would make a stored score depend on whether the analyzer ran.
        """
        repository = self.context.resolve_repository()
        written = 0
        for analysis in analyses:
            if not analysis.lead_id:
                continue
            lead = repository.get(analysis.lead_id)
            if lead is None:
                continue
            lead.raw["website_analysis"] = analysis.to_dict()
            repository.add(lead)
            written += 1
        return written


__all__ = [
    "WebsiteAnalyzerAgent",
    "WebsiteAnalysis",
    "WebsiteFinding",
    "load_analysis_config",
    "SEVERITY_ORDER",
    "ATTENTION_SEVERITIES",
    "INCONCLUSIVE_STATUSES",
]
