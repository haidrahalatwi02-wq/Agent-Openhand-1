"""Shared CLI output helpers (table rendering, formatting)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

#: Terminal width used when we cannot detect one.
DEFAULT_WIDTH = 140


def _visible_length(text: str) -> int:
    return len(str(text))


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    max_width: int = DEFAULT_WIDTH,
    max_column: int = 38,
) -> str:
    """Render a simple ASCII table, truncating wide columns.

    Implemented locally rather than pulling in a dependency: the CLI is part of
    the MVP and output should look reasonable in any terminal.
    """
    rendered: List[List[str]] = []
    for row in rows:
        rendered.append(
            [
                _truncate(str(cell) if cell is not None else "-", max_column)
                for cell in row
            ]
        )

    widths = [_visible_length(h) for h in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], _visible_length(cell))

    # Shrink the widest columns until the table fits.
    while sum(widths) + 3 * max(0, len(widths) - 1) > max_width and max(widths) > 12:
        widest = widths.index(max(widths))
        widths[widest] -= 2
        for row in rendered:
            if widest < len(row):
                row[widest] = _truncate(row[widest], widths[widest])

    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    separator = "-+-".join("-" * widths[i] for i in range(len(headers)))
    lines = [header_line.rstrip(), separator]
    for row in rendered:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "\u2026"


LEAD_HEADERS = ("Business", "City", "Website", "Website Status", "Score", "Confidence", "Priority")


def lead_rows(leads: Iterable[Any]) -> List[List[str]]:
    """Rows for the standard lead table."""
    return [lead.summary_row() for lead in leads]


def format_lead_detail(lead: Any) -> str:
    """Multi-line detail view for a single lead."""
    lines = [
        f"Business        : {lead.business_name}",
        f"ID              : {lead.id}",
        f"Type            : {lead.business_type or '-'}",
        f"Location        : {', '.join(p for p in (lead.city, lead.country) if p) or '-'}",
        f"Address         : {lead.address or '-'}",
        f"Phone           : {lead.phone or '-'}",
        f"Email           : {lead.email or '-'}",
        f"Website         : {lead.website_url or '-'}",
        f"Website status  : {lead.website_status}",
        f"Website quality : {lead.website_quality}",
        f"Social          : {', '.join(f'{k}: {v}' for k, v in lead.social_links.items()) or '-'}",
        f"Source          : {lead.source or '-'}",
        f"Source URL      : {lead.source_url or '-'}",
        f"Score           : {lead.lead_score} ({lead.score_confidence} confidence, {lead.priority})",
        f"Discovered at   : {lead.discovered_at}",
        f"Last checked    : {lead.last_checked_at or '-'}",
        "Reasons:",
    ]
    for reason in lead.score_reason or ["(none)"]:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def format_stats(stats: Dict[str, Any]) -> str:
    """Human-readable pipeline statistics."""
    lines = ["Pipeline summary", "----------------"]
    for key, value in stats.items():
        if key == "providers":
            lines.append("Providers:")
            for provider in value or []:
                status = (
                    f"error: {provider.get('error')}"
                    if provider.get("error")
                    else (
                        f"skipped: {provider.get('skipped_reason')}"
                        if provider.get("skipped_reason")
                        else f"{provider.get('count')} result(s)"
                    )
                )
                lines.append(f"  - {provider.get('provider')}: {status}")
            continue
        lines.append(f"{key:20}: {value}")
    return "\n".join(lines)


__all__ = [
    "render_table",
    "lead_rows",
    "format_lead_detail",
    "format_stats",
    "LEAD_HEADERS",
]