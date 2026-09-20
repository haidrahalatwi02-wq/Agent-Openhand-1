"""Editable non-secret runtime settings.

The dashboard can change the settings a human is allowed to change without
editing files or restarting with new environment variables. Values are stored as
an **overlay** applied on top of :func:`~lead_finder_agent.config.settings.get_settings`,
so the env-first design still wins for deployment-time configuration and nothing
here can silently contradict what a file says.

The schema is a whitelist. A field that is not listed cannot be written, which
does two things: it keeps obviously dangerous keys out of reach, and it makes it
mechanically true that **no credential can be stored here** — none of the
exposed fields is a secret. :class:`Settings` deliberately holds no secret
fields at all (the Google Places key is read from the environment inside the
provider), so the overlay has nothing secret to leak.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from lead_finder_agent.config.settings import Settings, get_settings
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.runtime_settings")

SETTINGS_FILENAME = "settings.json"

#: Every editable setting, with the type used to coerce an incoming value.
#: ``str`` fields are free text; ``enum`` fields are constrained to ``choices``.
SCHEMA: List[Dict[str, Any]] = [
    # General
    {"name": "default_country", "label": "Default country", "type": "str", "group": "General"},
    {"name": "default_city", "label": "Default city", "type": "str", "group": "General"},
    {
        "name": "providers",
        "label": "Search providers",
        "type": "list",
        "group": "General",
        "help": "Provider names, in order of use.",
    },
    {
        "name": "log_level",
        "label": "Log level",
        "type": "enum",
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
        "group": "General",
    },
    {"name": "db_path", "label": "Database path", "type": "path", "group": "General"},
    # Network
    {"name": "http_timeout", "label": "HTTP timeout (s)", "type": "float", "group": "Network"},
    {"name": "max_retries", "label": "Max retries", "type": "int", "group": "Network"},
    {"name": "overpass_url", "label": "Overpass endpoint", "type": "str", "group": "Network"},
    {"name": "nominatim_url", "label": "Nominatim endpoint", "type": "str", "group": "Network"},
    # Website checker
    {
        "name": "website_check_timeout",
        "label": "Website check timeout (s)",
        "type": "float",
        "group": "Website checker",
    },
    {
        "name": "website_max_redirects",
        "label": "Max redirects",
        "type": "int",
        "group": "Website checker",
    },
    {
        "name": "website_max_response_size",
        "label": "Max response size (bytes)",
        "type": "int",
        "group": "Website checker",
    },
    {
        "name": "website_cache_enabled",
        "label": "Cache checks per run",
        "type": "bool",
        "group": "Website checker",
    },
    # OpenStreetMap
    {
        "name": "osm_overpass_timeout",
        "label": "Overpass query budget (s)",
        "type": "int",
        "group": "OpenStreetMap",
    },
    {"name": "osm_max_elements", "label": "Max elements", "type": "int", "group": "OpenStreetMap"},
    {"name": "osm_oversample", "label": "Oversample factor", "type": "float", "group": "OpenStreetMap"},
    # Google Places (non-secret knobs only)
    {
        "name": "google_places_endpoint",
        "label": "Google Places endpoint",
        "type": "str",
        "group": "Google Places",
    },
    {
        "name": "google_places_max_pages",
        "label": "Max pages",
        "type": "int",
        "group": "Google Places",
    },
    {
        "name": "google_places_language",
        "label": "Language hint",
        "type": "str",
        "group": "Google Places",
    },
]


def editable_names() -> List[str]:
    return [entry["name"] for entry in SCHEMA]


def schema_by_group() -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in SCHEMA:
        grouped.setdefault(str(entry.get("group") or "General"), []).append(entry)
    return grouped


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


class RuntimeSettingsStore:
    """A persisted overlay of non-secret settings.

    Thread-safe. Unknown or non-whitelisted keys are rejected rather than
    stored, so the file cannot accumulate values the schema does not describe.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / SETTINGS_FILENAME
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None

    # -- storage -----------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data: Dict[str, Any] = {}
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                    if isinstance(raw, dict):
                        allowed = set(editable_names())
                        data = {k: _json_safe(v) for k, v in raw.items() if k in allowed}
            except (ValueError, OSError) as exc:
                log.warning("Could not read %s (%s); using defaults", self.path, exc)
                data = {}
            self._cache = data
            return data

    def _save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            self._cache = dict(data)

    def overrides(self) -> Dict[str, Any]:
        return dict(self._load())

    # -- coercion ----------------------------------------------------------

    @staticmethod
    def _coerce(entry: Mapping[str, Any], value: Any) -> Any:
        kind = entry.get("type")
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "list":
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if str(v).strip()]
            return [part.strip() for part in str(value).split(",") if part.strip()]
        if kind == "enum":
            text = str(value).strip()
            choices = [str(c) for c in entry.get("choices") or []]
            if text not in choices:
                raise ValueError(
                    f"{entry['name']} must be one of: {', '.join(choices)}"
                )
            return text
        if kind == "path":
            return str(Path(str(value)).expanduser())
        return str(value).strip()

    # -- api ---------------------------------------------------------------

    def update(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate and store ``values``, returning the new overlay.

        A value of ``None`` or an empty string clears the override, restoring
        the underlying environment/default value. That is how the UI offers
        "reset to default" without a separate endpoint.
        """
        by_name = {entry["name"]: entry for entry in SCHEMA}
        unknown = sorted(set(values) - set(by_name))
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
        with self._lock:
            data = dict(self._load())
            for name, raw in values.items():
                coerced = self._coerce(by_name[name], raw)
                if coerced is None:
                    data.pop(name, None)
                else:
                    data[name] = coerced
            self._save(data)
            return dict(data)

    def clear(self) -> None:
        with self._lock:
            self._save({})

    # -- effective view ----------------------------------------------------

    def effective(self) -> Dict[str, Any]:
        """Current values (base settings plus overlay), grouped for the UI.

        Reads the base through :func:`get_settings` so the dashboard reports the
        values the application would actually use, not a second copy.
        """
        base = get_settings()
        overlay = self._load()
        out: Dict[str, Any] = {}
        for entry in SCHEMA:
            name = entry["name"]
            if name in overlay:
                value: Any = overlay[name]
                source = "override"
            else:
                value = _json_safe(getattr(base, name, None))
                source = "default"
            out[name] = {
                "value": value,
                "source": source,
                "type": entry.get("type"),
                "label": entry.get("label"),
                "group": entry.get("group") or "General",
            }
        return out

    def apply(self, settings: Optional[Settings] = None) -> Settings:
        """Return ``settings`` with the overlay applied.

        Keys the underlying :class:`Settings` does not define are ignored, so a
        stale overlay written by a newer dashboard cannot break an older one.
        """
        base = settings or get_settings()
        overlay = self._load()
        valid = {
            key: value
            for key, value in overlay.items()
            if hasattr(base, key)
        }
        if not valid:
            return base
        try:
            return replace(base, **valid)
        except TypeError as exc:  # pragma: no cover - only on a schema mismatch
            log.warning("Ignoring invalid settings overlay: %s", exc)
            return base


__all__ = [
    "RuntimeSettingsStore",
    "SCHEMA",
    "SETTINGS_FILENAME",
    "editable_names",
    "schema_by_group",
]