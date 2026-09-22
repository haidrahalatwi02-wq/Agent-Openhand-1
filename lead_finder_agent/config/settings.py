"""Runtime settings.

Settings come from environment variables (optionally seeded from a ``.env``
file) with sensible defaults, so the agent runs with no configuration at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from lead_finder_agent.config.loader import PROJECT_ROOT, resolve_path
from lead_finder_agent.utils.http import DEFAULT_USER_AGENT
from lead_finder_agent.utils.text import parse_keywords, safe_int


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def load_dotenv(path: str | Path = ".env") -> bool:
    """Load a ``.env`` file into ``os.environ``.

    Uses ``python-dotenv`` when installed; otherwise falls back to a minimal
    parser that ignores comments and blank lines. Existing environment variables
    are never overwritten.
    """
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = PROJECT_ROOT / file_path
    if not file_path.exists():
        return False

    try:  # pragma: no cover - depends on environment
        from dotenv import load_dotenv as _dotenv_load  # noqa: WPS433

        _dotenv_load(dotenv_path=file_path, override=False)
        return True
    except ImportError:
        pass

    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
    return True


@dataclass
class Settings:
    """Effective configuration for a run."""

    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "leads.db")
    default_country: Optional[str] = "Yemen"
    default_city: Optional[str] = "Aden"
    providers: List[str] = field(default_factory=lambda: ["osm", "sample"])
    user_agent: str = DEFAULT_USER_AGENT
    http_timeout: float = 12.0
    max_retries: int = 1
    log_level: str = "INFO"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    scoring_rules_path: Optional[Path] = None
    business_types_path: Optional[Path] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        """Build settings from a mapping (defaults to ``os.environ``)."""
        source = env if env is not None else dict(os.environ)

        def get(key: str, default: Optional[str] = None) -> Optional[str]:
            value = source.get(key)
            return value if value not in (None, "") else default

        db_raw = get("LEAD_FINDER_DB_PATH", "data/leads.db")
        timeout = get("LEAD_FINDER_HTTP_TIMEOUT", "12")
        retries = get("LEAD_FINDER_MAX_RETRIES", "1")
        providers = parse_keywords(get("LEAD_FINDER_PROVIDERS", "osm,sample"))
        rules_path = get("LEAD_FINDER_SCORING_RULES")
        types_path = get("LEAD_FINDER_BUSINESS_TYPES")

        return cls(
            db_path=resolve_path(db_raw or "data/leads.db"),
            default_country=get("LEAD_FINDER_DEFAULT_COUNTRY", "Yemen"),
            default_city=get("LEAD_FINDER_DEFAULT_CITY", "Aden"),
            providers=providers or ["osm", "sample"],
            user_agent=get("LEAD_FINDER_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
            http_timeout=float(timeout) if timeout else 12.0,
            max_retries=safe_int(retries, 1) or 0,
            log_level=(get("LEAD_FINDER_LOG_LEVEL", "INFO") or "INFO").upper(),
            overpass_url=get(
                "LEAD_FINDER_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
            )
            or "https://overpass-api.de/api/interpreter",
            nominatim_url=get(
                "LEAD_FINDER_NOMINATIM_URL", "https://nominatim.openstreetmap.org"
            )
            or "https://nominatim.openstreetmap.org",
            scoring_rules_path=resolve_path(rules_path) if rules_path else None,
            business_types_path=resolve_path(types_path) if types_path else None,
        )

    def with_overrides(self, **kwargs: Any) -> "Settings":
        """Return a copy with selected fields replaced."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)

    def ensure_directories(self) -> None:
        """Create the parent directory of the database if it is missing."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_SETTINGS: Optional[Settings] = None


def get_settings(force_reload: bool = False) -> Settings:
    """Return the process-wide settings singleton."""
    global _SETTINGS
    if _SETTINGS is None or force_reload:
        load_dotenv()
        _SETTINGS = Settings.from_env()
    return _SETTINGS


def reset_settings() -> None:
    """Clear the cached settings (used by tests)."""
    global _SETTINGS
    _SETTINGS = None


__all__ = ["Settings", "get_settings", "reset_settings", "load_dotenv"]
