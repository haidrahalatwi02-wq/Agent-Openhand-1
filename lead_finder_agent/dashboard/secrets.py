"""Secret storage and masking.

Secrets reach the dashboard from exactly two places, in this order of
precedence:

1. **The environment.** If a variable is set, that value wins and the dashboard
   reports the source as ``environment``. This keeps the project's env-first
   design intact and means a deployment can inject credentials without the
   dashboard being able to overwrite them.
2. **A local file** under the dashboard data directory, written mode ``0600``,
   for values entered through the UI during local use.

Two rules are non-negotiable, and they are enforced here rather than at each
call site:

* **No accessor returns a value for display.** :meth:`SecretStore.describe` is
  the only method the API layer uses, and it returns a masked preview and a
  boolean — never the secret. The raw value is reachable only through
  :meth:`SecretStore.get`, which exists solely so a component that genuinely
  needs the credential (a provider building an auth header) can read it.
* **Nothing is logged.** This module never logs a value, and
  :meth:`SecretStore.redact` exists so error text produced elsewhere can be
  scrubbed before it is returned or written anywhere.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

#: File name inside the dashboard data directory.
SECRETS_FILENAME = "secrets.json"

SOURCE_ENVIRONMENT = "environment"
SOURCE_LOCAL = "local"

_MASK = "\u2022" * 8


def mask(value: Optional[str]) -> str:
    """Return a non-recoverable preview of ``value``.

    Long values keep their last four characters so a human can tell *which* key
    is installed without being able to reconstruct it. Short values are masked
    entirely, because for a short secret the tail is a meaningful fraction of
    the whole thing.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) < 12:
        return _MASK
    return _MASK + text[-4:]


@dataclass(frozen=True)
class SecretState:
    """What the dashboard is allowed to know about a secret."""

    configured: bool
    source: Optional[str] = None
    preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "source": self.source,
            "preview": self.preview,
        }


class SecretStore:
    """Reads secrets from the environment, falling back to a local file.

    Thread-safe: the dashboard serves requests from several threads while a
    background job may also be reading a credential.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / SECRETS_FILENAME
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, str]] = None

    # -- storage -----------------------------------------------------------

    def _load(self) -> Dict[str, str]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data: Dict[str, str] = {}
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                    if isinstance(raw, dict):
                        data = {str(k): str(v) for k, v in raw.items() if v}
            except (ValueError, OSError):
                # A corrupt or unreadable secret file must not crash the
                # dashboard: the environment is still a valid source, and the
                # next write repairs the file.
                data = {}
            self._cache = data
            return data

    def _save(self, data: Dict[str, str]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(data, indent=2, sort_keys=True)
            # Create with restrictive permissions before any content is written,
            # so the secret is never briefly world-readable.
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            finally:
                os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(self.path))
            os.chmod(str(self.path), 0o600)
            self._cache = dict(data)

    # -- reading -----------------------------------------------------------

    def get(self, name: str) -> Optional[str]:
        """Return the raw secret, or ``None``.

        This is the *only* method returning a real value. Callers that only need
        to display status must use :meth:`describe` instead.
        """
        if not name:
            return None
        env = os.getenv(name)
        if env:
            return env
        return self._load().get(name)

    def describe(self, name: str) -> SecretState:
        """Masked state of ``name``. Safe to return from an API response."""
        value = self.get(name)
        if not value:
            return SecretState(configured=False)
        source = SOURCE_ENVIRONMENT if os.getenv(name) else SOURCE_LOCAL
        return SecretState(configured=True, source=source, preview=mask(value))

    def is_configured(self, name: str) -> bool:
        return bool(self.get(name))

    # -- writing -----------------------------------------------------------

    def set(self, name: str, value: str) -> SecretState:
        """Store ``value`` locally.

        If the same name is present in the environment, the environment keeps
        winning and the stored value is inert. Reporting that honestly is better
        than silently pretending the write took effect.
        """
        name = str(name).strip()
        if not name:
            raise ValueError("A secret name is required")
        with self._lock:
            data = dict(self._load())
            data[name] = str(value)
            self._save(data)
        return self.describe(name)

    def delete(self, name: str) -> bool:
        """Remove the locally stored value. An environment variable is unaffected."""
        name = str(name).strip()
        with self._lock:
            data = dict(self._load())
            if name not in data:
                return False
            data.pop(name, None)
            self._save(data)
        return True

    def local_names(self) -> Iterable[str]:
        return tuple(sorted(self._load().keys()))

    # -- hygiene -----------------------------------------------------------

    def redact(self, text: Any) -> str:
        """Replace any known secret value found in ``text`` with a mask.

        Used on error messages before they are logged or returned, so a failure
        that happens to quote a credential cannot leak it.
        """
        result = "" if text is None else str(text)
        if not result:
            return result
        candidates = set(self._load().values())
        for key in ("GOOGLE_PLACES_API_KEY", "SERPAPI_API_KEY"):
            value = os.getenv(key)
            if value:
                candidates.add(value)
        # Longest first, so a secret that contains another is masked whole.
        for value in sorted(candidates, key=len, reverse=True):
            if value and value in result:
                result = result.replace(value, mask(value))
        return result


__all__ = [
    "SecretStore",
    "SecretState",
    "mask",
    "SECRETS_FILENAME",
    "SOURCE_ENVIRONMENT",
    "SOURCE_LOCAL",
]