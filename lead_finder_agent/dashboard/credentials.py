"""Central provider and credential catalog.

This is the single place the dashboard learns which providers exist and which
credential (if any) each one needs. It exists so that:

* the search-provider section is **derived from the project's own provider
  registry** rather than restated here, so adding a provider or a keyed
  capability does not require editing the dashboard; and
* every secret name is declared exactly once, which is what makes the
  "no secret leaks" test meaningful — the test can enumerate this catalog and
  assert that none of the values appear in any API response.

Agents own *instructions and settings*. Credentials are owned centrally here
and are referenced by name only, so no agent has an independent credential
system and no agent ever stores or returns a secret value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from lead_finder_agent.search.registry import registry as provider_registry

#: Credential-free channels are listed so the UI can show them as "no key
#: required" instead of omitting them, which would read as "not supported".
CHANNEL_SEARCH = "search"
CHANNEL_LLM = "llm"
CHANNEL_EMAIL = "email"
CHANNEL_MESSAGING = "messaging"


@dataclass(frozen=True)
class ProviderCredential:
    """One configurable provider and the credential it may require."""

    key: str
    label: str
    channel: str
    #: Environment variable holding the secret, or ``None`` when keyless.
    secret_env: Optional[str] = None
    #: Which component reads this value. Purely descriptive, for the UI.
    used_by: str = ""
    description: str = ""
    #: Fields that are *not* secret but are part of configuring the provider.
    settings: Sequence[str] = field(default_factory=tuple)
    #: True for providers that ship with the project and work today.
    implemented: bool = True

    @property
    def requires_secret(self) -> bool:
        return bool(self.secret_env)

    def to_dict(self) -> Dict[str, Any]:
        """Describe this provider without its credential value."""
        return {
            "key": self.key,
            "label": self.label,
            "channel": self.channel,
            "secret_env": self.secret_env,
            "requires_secret": self.requires_secret,
            "used_by": self.used_by,
            "description": self.description,
            "settings": list(self.settings),
            "implemented": self.implemented,
        }


#: Providers whose credential is declared here. Search providers are merged in
#: from the live registry by :func:`catalog`, so this list only needs the
#: non-search channels plus the search providers that need a key.
_STATIC: tuple[ProviderCredential, ...] = (
    # -- Search -----------------------------------------------------------
    ProviderCredential(
        key="google_places",
        label="Google Places (New)",
        channel=CHANNEL_SEARCH,
        # Matches the provider's own default so the two cannot drift.
        secret_env="GOOGLE_PLACES_API_KEY",
        used_by="search.providers.google_places",
        description=(
            "Paid, keyed business discovery. Skipped automatically when no key "
            "is set, so it is safe to leave enabled."
        ),
        settings=(
            "LEAD_FINDER_GOOGLE_PLACES_ENDPOINT",
            "LEAD_FINDER_GOOGLE_PLACES_MAX_PAGES",
            "LEAD_FINDER_GOOGLE_PLACES_LANGUAGE",
        ),
    ),
    # -- LLM --------------------------------------------------------------
    ProviderCredential(
        key="openai",
        label="OpenAI",
        channel=CHANNEL_LLM,
        secret_env="OPENAI_API_KEY",
        used_by="(reserved - no agent uses an LLM yet)",
        description=(
            "Declared so the channel exists before it is needed. No current "
            "agent calls an LLM; the Lead Finder and Website Analyzer are "
            "deterministic and run offline."
        ),
        settings=("OPENAI_MODEL",),
        implemented=False,
    ),
    ProviderCredential(
        key="anthropic",
        label="Anthropic",
        channel=CHANNEL_LLM,
        secret_env="ANTHROPIC_API_KEY",
        used_by="(reserved - no agent uses an LLM yet)",
        description="Reserved LLM channel. Not required by any current agent.",
        settings=("ANTHROPIC_MODEL",),
        implemented=False,
    ),
    ProviderCredential(
        key="custom_llm",
        label="Custom / self-hosted LLM",
        channel=CHANNEL_LLM,
        secret_env="LEAD_FINDER_CUSTOM_API_KEY",
        used_by="(reserved)",
        description="Placeholder for a self-hosted or proxy endpoint.",
        settings=("LEAD_FINDER_LLM_BASE_URL",),
        implemented=False,
    ),
    # -- Email ------------------------------------------------------------
    ProviderCredential(
        key="smtp",
        label="SMTP (email sending)",
        channel=CHANNEL_EMAIL,
        secret_env="SMTP_PASSWORD",
        used_by="(reserved - no agent sends email yet)",
        description=(
            "Reserved for the outreach phase. Host/port/username are plain "
            "settings; only the password is a secret."
        ),
        settings=("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_FROM"),
        implemented=False,
    ),
    # -- Messaging --------------------------------------------------------
    ProviderCredential(
        key="whatsapp",
        label="WhatsApp Business API",
        channel=CHANNEL_MESSAGING,
        secret_env="WHATSAPP_ACCESS_TOKEN",
        used_by="(reserved - no agent sends messages yet)",
        description=(
            "Reserved messaging channel. Only the access token is a secret; "
            "the phone number id and business id are settings."
        ),
        settings=("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID"),
        implemented=False,
    ),
)


def _search_credentials() -> List[ProviderCredential]:
    """Search providers, derived from the project's own registry.

    A provider that needs no key is still listed (with ``secret_env=None``), so
    the dashboard reflects what is actually installed rather than a hand-kept
    copy that can fall out of date.
    """
    known = {entry.key: entry for entry in _STATIC}
    discovered: List[ProviderCredential] = []
    try:
        names = provider_registry().names()
    except Exception:  # pragma: no cover - registry is populated at import
        return []
    for name in names:
        if name in known:
            discovered.append(known[name])
            continue
        discovered.append(
            ProviderCredential(
                key=name,
                label=name.replace("_", " ").title(),
                channel=CHANNEL_SEARCH,
                secret_env=None,
                used_by=f"search.providers.{name}",
                description="Search provider registered with the project. No key required.",
            )
        )
    return discovered


def catalog() -> List[ProviderCredential]:
    """Every configurable provider, search providers first."""
    entries = _search_credentials()
    seen = {entry.key for entry in entries}
    for entry in _STATIC:
        if entry.key not in seen and entry.channel != CHANNEL_SEARCH:
            entries.append(entry)
    return entries


def by_channel() -> Dict[str, List[ProviderCredential]]:
    grouped: Dict[str, List[ProviderCredential]] = {}
    for entry in catalog():
        grouped.setdefault(entry.channel, []).append(entry)
    return grouped


def find(key: str) -> Optional[ProviderCredential]:
    target = str(key or "").strip().lower()
    for entry in catalog():
        if entry.key == target:
            return entry
    return None


def resolve_secret_name(name: str) -> str:
    """Map a caller-supplied name to the one canonical secret name.

    Accepts either a provider key (``google_places``) or the environment
    variable (``GOOGLE_PLACES_API_KEY``) and returns the environment variable,
    because that is the name the provider reads at run time.

    Only names declared in the catalog are accepted. The dashboard exists to
    manage the credentials the project actually uses; accepting arbitrary names
    would let a caller write an unused key into the store and read back a
    success that means nothing — and it would grow the set of names the
    secret-leak tests must reason about.

    Raises:
        ValueError: for an empty name, an unknown name, or a provider that
            takes no credential.
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("A credential name is required")

    if target in secret_names():
        return target

    entry = find(target)
    if entry is None:
        raise ValueError(f"Unknown credential {target!r}")
    if not entry.secret_env:
        raise ValueError(f"{entry.label} does not require a credential")
    return entry.secret_env


def secret_names() -> List[str]:
    """Every secret env-var name the dashboard knows about.

    The secret-leak tests iterate this so a newly declared credential is
    automatically covered.
    """
    return [entry.secret_env for entry in catalog() if entry.secret_env]


__all__ = [
    "ProviderCredential",
    "catalog",
    "by_channel",
    "find",
    "resolve_secret_name",
    "secret_names",
    "CHANNEL_SEARCH",
    "CHANNEL_LLM",
    "CHANNEL_EMAIL",
    "CHANNEL_MESSAGING",
]