"""Export stored leads to JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from lead_finder_agent.models import Lead
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("storage.exporters")

#: Column order for CSV output. Flat, spreadsheet friendly, one row per lead.
CSV_COLUMNS: Sequence[str] = (
    "id",
    "business_name",
    "business_type",
    "country",
    "city",
    "address",
    "phone",
    "email",
    "source",
    "source_url",
    "sources",
    "provider_ids",
    "website_url",
    "website_status",
    "website_quality",
    "social_links",
    "description",
    "lead_score",
    "score_confidence",
    "priority",
    "score_reason",
    "discovered_at",
    "last_checked_at",
)


def _flatten(lead: Lead) -> Dict[str, Any]:
    data = lead.to_dict()
    row = {column: data.get(column) for column in CSV_COLUMNS}
    # Spreadsheets and shell tools read these far more easily as text than as
    # the Python repr a nested list or dict would produce.
    sources = row.get("sources")
    if isinstance(sources, (list, tuple)):
        row["sources"] = ",".join(str(s) for s in sources)
    provider_ids = row.get("provider_ids")
    if isinstance(provider_ids, dict):
        row["provider_ids"] = ";".join(f"{k}={v}" for k, v in provider_ids.items())
    return row


def export_json(
    leads: Iterable[Lead],
    indent: Optional[int] = 2,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Serialize leads to a JSON document."""
    items = list(leads)
    payload: Any
    if metadata is None:
        payload = [lead.to_dict() for lead in items]
    else:
        payload = {
            "metadata": {**metadata, "count": len(items)},
            "leads": [lead.to_dict() for lead in items],
        }
    return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


def export_csv(leads: Iterable[Lead]) -> str:
    """Serialize leads to CSV text.

    List and dict fields are flattened into ``|`` separated values so a row
    stays on one line and opens cleanly in a spreadsheet.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = _flatten(lead)
        for key in ("social_links", "score_reason"):
            value = row.get(key)
            if isinstance(value, dict):
                row[key] = " | ".join(f"{k}: {v}" for k, v in value.items())
            elif isinstance(value, (list, tuple)):
                row[key] = " | ".join(str(item) for item in value)
        writer.writerow(row)
    return buffer.getvalue()


def write_json(
    leads: Iterable[Lead],
    path: str | Path,
    indent: Optional[int] = 2,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write leads to a JSON file, creating parent directories as needed."""
    items = list(leads)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(export_json(items, indent=indent, metadata=metadata) + "\n", encoding="utf-8")
    log.info("Wrote %s leads to %s", len(items), target)
    return target


def write_csv(leads: Iterable[Lead], path: str | Path) -> Path:
    """Write leads to a CSV file, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(export_csv(leads), encoding="utf-8")
    return target


def export_leads(
    leads: Iterable[Lead],
    path: str | Path,
    fmt: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Export leads using the format from ``fmt`` or the file extension."""
    items = list(leads)
    target = Path(path)
    resolved = (fmt or target.suffix.lstrip(".") or "json").lower()
    if resolved == "json":
        return write_json(items, target, metadata=metadata)
    if resolved in {"csv", "tsv"}:
        if resolved == "tsv":
            text = export_csv(items).replace(",", "\t")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return target
        return write_csv(items, target)
    raise ValueError(f"Unsupported export format: {fmt!r} (use 'json' or 'csv')")


__all__ = [
    "export_json",
    "export_csv",
    "write_json",
    "write_csv",
    "export_leads",
    "CSV_COLUMNS",
]
