"""Configuration file loading helpers.

YAML support is optional: if ``PyYAML`` is not installed, ``.yaml`` files fail
with a clear message and JSON files (or the packaged JSON defaults) are used
instead. This keeps the runtime dependency list at exactly one package.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("config.loader")

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent


def config_dir() -> Path:
    """Directory holding packaged default configuration data."""
    return PACKAGE_ROOT / "data"


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "PyYAML is required to read YAML configuration. "
            "Install it with `pip install PyYAML` or use a JSON file instead."
        ) from exc
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle) or {}


def load_config_file(path: str | os.PathLike[str]) -> Dict[str, Any]:
    """Load a YAML or JSON config file based on its extension."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return _read_yaml(file_path)
    if suffix == ".json":
        return _read_json(file_path)
    raise ValueError(f"Unsupported configuration format: {file_path.suffix}")


def load_packaged_data(name: str) -> Dict[str, Any]:
    """Load a packaged default from ``lead_finder_agent/config/data``.

    ``name`` may be given without an extension; YAML is preferred, JSON is the
    fallback so the project still works without PyYAML installed.
    """
    base = config_dir() / Path(name).stem
    yaml_path = base.with_suffix(".yaml")
    json_path = base.with_suffix(".json")

    if yaml_path.exists():
        try:
            return _read_yaml(yaml_path)
        except RuntimeError as exc:
            log.debug("Falling back to JSON for %s: %s", name, exc)
    if json_path.exists():
        return _read_json(json_path)
    raise FileNotFoundError(f"No packaged configuration named {name!r} in {config_dir()}")


def merge_dicts(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either."""
    result: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], Mapping)
            and isinstance(value, Mapping)
        ):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_path(path: str | os.PathLike[str], base: Optional[Path] = None) -> Path:
    """Resolve a possibly-relative path against ``base`` (default: repo root)."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (base or PROJECT_ROOT) / candidate


__all__ = [
    "load_config_file",
    "load_packaged_data",
    "merge_dicts",
    "resolve_path",
    "config_dir",
    "PROJECT_ROOT",
]
