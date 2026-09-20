"""Dashboard tests: credentials, secret handling, and leak prevention.

These are the tests that matter most, so they are deliberately adversarial: they
set a recognisable sentinel secret, drive the real API, and then assert the
sentinel appears **nowhere** in any response, log record, or file the dashboard
wrote.

The last test is the broad one: it iterates every endpoint the router exposes
and every secret name the credential catalog declares, so a credential added in
future is covered automatically without editing this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

from lead_finder_agent.dashboard.api import ApiApp
from lead_finder_agent.dashboard.credentials import (
    catalog,
    resolve_secret_name,
    secret_names,
)
from lead_finder_agent.dashboard.secrets import SecretStore, mask
from lead_finder_agent.dashboard.service import DashboardService

#: A value distinctive enough that finding it anywhere is unambiguous evidence
#: of a leak. Not a real credential, and never used against a real service.
SENTINEL = "sentinel-secret-value-9f3a7c1e-DO-NOT-LEAK"


@pytest.fixture
def service(tmp_path: Path) -> DashboardService:
    svc = DashboardService(data_dir=tmp_path / "dash")
    yield svc
    svc.close()


@pytest.fixture
def app(service: DashboardService) -> ApiApp:
    return ApiApp(service)


class TestMasking:
    def test_short_value_is_fully_masked(self):
        assert mask("abc") == "\u2022" * 8
        assert "abc" not in mask("abc")

    def test_long_value_keeps_only_a_tail_hint(self):
        masked = mask("sk-abcdefghijklmnop")
        assert masked.endswith("mnop")
        assert "abcdefghij" not in masked
        assert masked.startswith("\u2022" * 8)

    def test_empty_value_masks_to_empty(self):
        assert mask("") == ""
        assert mask(None) == ""


class TestSecretStore:
    def test_absent_secret_reports_not_configured(self, tmp_path: Path):
        store = SecretStore(tmp_path)
        state = store.describe("NOT_SET_ANYWHERE")
        assert state.configured is False
        assert state.preview == ""
        assert state.source is None

    def test_environment_takes_precedence(self, tmp_path: Path, monkeypatch):
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", "local-value-here")
        monkeypatch.setenv("MY_TEST_SECRET", "environment-value-here")
        assert store.get("MY_TEST_SECRET") == "environment-value-here"
        assert store.describe("MY_TEST_SECRET").source == "environment"

    def test_local_value_is_used_when_no_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", "local-only-value-abc")
        assert store.get("MY_TEST_SECRET") == "local-only-value-abc"
        assert store.describe("MY_TEST_SECRET").source == "local"

    def test_describe_never_returns_the_value(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", SENTINEL)
        state = store.describe("MY_TEST_SECRET")
        assert SENTINEL not in json.dumps(state.to_dict())

    def test_stored_file_is_owner_only(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", SENTINEL)
        mode = store.path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_delete_removes_local_value(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", "value-to-delete-here")
        assert store.delete("MY_TEST_SECRET") is True
        assert store.get("MY_TEST_SECRET") is None
        assert store.delete("MY_TEST_SECRET") is False

    def test_delete_does_not_touch_the_environment(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MY_TEST_SECRET", "from-environment-value")
        store = SecretStore(tmp_path)
        store.delete("MY_TEST_SECRET")
        assert store.get("MY_TEST_SECRET") == "from-environment-value"

    def test_corrupt_file_does_not_break_the_store(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "secrets.json").write_text("{ not json", encoding="utf-8")
        store = SecretStore(tmp_path)
        assert store.describe("MY_TEST_SECRET").configured is False

    def test_redact_removes_a_known_value_from_text(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MY_TEST_SECRET", raising=False)
        store = SecretStore(tmp_path)
        store.set("MY_TEST_SECRET", SENTINEL)
        cleaned = store.redact(f"request failed with key {SENTINEL} in the header")
        assert SENTINEL not in cleaned
        assert "\u2022" in cleaned

    def test_redact_leaves_unrelated_text_alone(self, tmp_path: Path):
        store = SecretStore(tmp_path)
        assert store.redact("a normal message") == "a normal message"


class TestCredentialCatalog:
    def test_catalog_covers_every_channel(self):
        channels = {entry.channel for entry in catalog()}
        assert {"search", "llm", "email", "messaging"} <= channels

    def test_search_providers_come_from_the_live_registry(self):
        """A provider in the project must appear in the catalog."""
        from lead_finder_agent.search.registry import registry

        keys = {entry.key for entry in catalog() if entry.channel == "search"}
        assert set(registry().names()) <= keys

    def test_google_places_secret_matches_the_providers_own_default(self):
        """The catalog must not invent a different variable name."""
        from lead_finder_agent.search.providers.google_places import (
            DEFAULT_API_KEY_ENV,
        )

        entry = next(e for e in catalog() if e.key == "google_places")
        assert entry.secret_env == DEFAULT_API_KEY_ENV

    def test_keyless_providers_are_marked_as_not_requiring_a_secret(self):
        osm = next((e for e in catalog() if e.key == "osm"), None)
        assert osm is not None
        assert osm.requires_secret is False
        assert osm.secret_env is None

    def test_catalog_dict_never_contains_a_secret_value(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", SENTINEL)
        for entry in catalog():
            assert SENTINEL not in json.dumps(entry.to_dict())


class TestCredentialApi:
    def test_list_shows_masked_state_only(self, app: ApiApp, monkeypatch):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", SENTINEL)
        response = app.handle("GET", "/api/credentials")
        assert response.status == 200
        body = json.dumps(response.payload)
        assert SENTINEL not in body
        google = next(p for p in response.payload["providers"] if p["key"] == "google_places")
        assert google["secret"]["configured"] is True
        assert google["secret"]["source"] == "environment"

    def test_put_stores_and_returns_only_masked_state(self, app: ApiApp, service, monkeypatch):
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        response = app.handle("PUT", "/api/credentials/SMTP_PASSWORD", body={"value": SENTINEL})
        assert response.status == 200
        assert SENTINEL not in json.dumps(response.payload)
        assert response.payload["configured"] is True
        # The value is genuinely stored, and readable by the component that
        # needs it, even though the API never returned it.
        assert service.secrets.get("SMTP_PASSWORD") == SENTINEL

    def test_put_rejects_a_missing_value(self, app: ApiApp):
        assert app.handle("PUT", "/api/credentials/SMTP_PASSWORD", body={}).status == 400
        assert app.handle("PUT", "/api/credentials/SMTP_PASSWORD", body={"value": "  "}).status == 400
        assert app.handle("PUT", "/api/credentials/SMTP_PASSWORD", body=None).status == 400

    def test_delete_removes_the_value(self, app: ApiApp, service, monkeypatch):
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        app.handle("PUT", "/api/credentials/SMTP_PASSWORD", body={"value": SENTINEL})
        response = app.handle("DELETE", "/api/credentials/SMTP_PASSWORD")
        assert response.status == 200
        assert response.payload["removed"] is True
        assert service.secrets.get("SMTP_PASSWORD") is None


class TestNoSecretLeaks:
    """The core guarantee: a configured secret is not observable via the API."""

    def test_prominent_secret_absent_from_every_read_endpoint(self, app: ApiApp, monkeypatch):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", SENTINEL)
        monkeypatch.setenv("SERPAPI_API_KEY", SENTINEL)
        reads = [
            "/api",
            "/api/health",
            "/api/overview",
            "/api/agents",
            "/api/agents/lead_finder",
            "/api/agents/website_analyzer",
            "/api/credentials",
            "/api/leads",
            "/api/leads/facets",
            "/api/runs",
            "/api/settings",
        ]
        for path in reads:
            response = app.handle("GET", path)
            payload = json.dumps(response.payload, default=str)
            assert SENTINEL not in payload, f"secret leaked from GET {path}"

    def test_secret_absent_from_every_declared_secret_name(self, app: ApiApp, monkeypatch):
        """Set every known secret, then check nothing exposes any of them.

        Adding a credential to the catalog automatically extends this test.
        """
        for name in secret_names():
            monkeypatch.setenv(name, SENTINEL)
        for path in ("/api/credentials", "/api/overview", "/api/health", "/api/settings"):
            response = app.handle("GET", path)
            assert SENTINEL not in json.dumps(response.payload, default=str), path

    def test_secret_absent_from_agent_endpoints(self, app: ApiApp, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
        response = app.handle("GET", "/api/agents")
        assert SENTINEL not in json.dumps(response.payload, default=str)

    def test_agent_config_cannot_absorb_a_credential(self, app: ApiApp):
        """Per-agent settings must refuse credential-shaped keys."""
        response = app.handle(
            "PUT",
            "/api/agents/lead_finder",
            body={"settings": {"api_key": SENTINEL, "openai_secret_token": "x"}},
        )
        assert response.status == 400
        assert "credentials" in response.payload["error"].lower()
        # And the value must not have been stored anywhere.
        stored = json.dumps(app.service.agent_config.all(), default=str)
        assert SENTINEL not in stored

    def test_secret_absent_from_logs(self, app: ApiApp, monkeypatch, caplog):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", SENTINEL)
        with caplog.at_level(logging.DEBUG):
            app.handle("GET", "/api/credentials")
            app.handle("GET", "/api/overview")
            app.handle("PUT", "/api/credentials/BAD_KEY_XYZ", body={"value": SENTINEL})
        assert SENTINEL not in caplog.text

    def test_error_text_is_scrubbed(self, app: ApiApp, service, monkeypatch):
        """An exception that quotes a secret must not leak it."""
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        service.set_credential("SMTP_PASSWORD", SENTINEL)
        assert SENTINEL not in service.redact(f"connect failed: {SENTINEL}")

    def test_secret_absent_from_files_written_by_the_dashboard(self, service, tmp_path, monkeypatch):
        """Only the dedicated secrets file may contain the value, mode 0600."""
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        service.set_credential("SMTP_PASSWORD", SENTINEL)
        service.update_settings({"default_city": "Aden"})
        service.update_agent("lead_finder", {"instructions": "Find leads"})
        service.overview()

        allowed = service.secrets.path.resolve()
        for path in service.data_dir.rglob("*"):
            if not path.is_file() or path.resolve() == allowed:
                continue
            assert SENTINEL not in path.read_text(encoding="utf-8", errors="replace"), path


def _js_code_without_comments() -> str:
    """The dashboard script with comments stripped.

    The checks below look for forbidden browser APIs. A comment that *mentions*
    ``localStorage`` (for example, one explaining that the script never uses it)
    is not a usage, so comments are removed first to avoid a false positive.
    """
    static = Path(__file__).resolve().parents[2] / "lead_finder_agent" / "dashboard" / "static"
    text = (static / "app.js").read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.MULTILINE)


class TestStaticAssets:
    """The frontend must not be a secret channel either."""

    def test_javascript_never_reads_a_secret_from_storage(self):
        code = _js_code_without_comments()
        for forbidden in ("localStorage", "sessionStorage", "document.cookie"):
            assert forbidden not in code, f"app.js uses {forbidden}"

    def test_javascript_does_not_console_log(self):
        code = _js_code_without_comments()
        assert "console.log" not in code

    def test_no_secret_is_embedded_in_the_frontend(self, monkeypatch):
        static = Path(__file__).resolve().parents[2] / "lead_finder_agent" / "dashboard" / "static"
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", SENTINEL)
        for asset in static.iterdir():
            if asset.is_file():
                assert SENTINEL not in asset.read_text(encoding="utf-8", errors="replace")

    def test_gitignore_excludes_dashboard_state(self):
        root = Path(__file__).resolve().parents[2]
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        assert "data/dashboard/" in ignored
        assert "secrets.json" in ignored


class TestEnvExample:
    def test_env_example_ships_no_real_credential(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if any(hint in key.lower() for hint in ("key", "token", "password", "secret")):
                assert value.strip() == "", f"{key} carries a value in .env.example"


class TestSecretNameResolution:
    """A saved credential must land on the name the provider actually reads.

    The browser posts the provider *key* (``google_places``) because that is the
    stable identifier shown in the UI. Providers read an environment variable.
    If the two are not reconciled, saving reports success while the key never
    reaches the provider, and the UI then displays the provider as unconfigured.
    """

    def test_provider_key_resolves_to_the_environment_variable(self):
        assert resolve_secret_name("google_places") == "GOOGLE_PLACES_API_KEY"

    def test_environment_variable_name_is_accepted_unchanged(self):
        assert resolve_secret_name("GOOGLE_PLACES_API_KEY") == "GOOGLE_PLACES_API_KEY"

    def test_surrounding_whitespace_is_ignored(self):
        assert resolve_secret_name("  google_places  ") == "GOOGLE_PLACES_API_KEY"

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_empty_name_is_rejected(self, name):
        with pytest.raises(ValueError):
            resolve_secret_name(name)

    def test_unknown_name_is_rejected(self):
        """Only credentials the project declares may be written.

        Accepting an arbitrary name would let a caller store an unused value and
        read back a success that means nothing.
        """
        with pytest.raises(ValueError):
            resolve_secret_name("TOTALLY_UNKNOWN_SECRET")

    def test_keyless_provider_is_rejected(self):
        with pytest.raises(ValueError):
            resolve_secret_name("osm")

    def test_put_with_a_provider_key_configures_the_provider(self, app: ApiApp, service, monkeypatch):
        """The end-to-end path the UI takes."""
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        response = app.handle("PUT", "/api/credentials/google_places", body={"value": SENTINEL})
        assert response.status == 200
        assert response.payload["name"] == "GOOGLE_PLACES_API_KEY"
        assert SENTINEL not in json.dumps(response.payload)
        assert service.secrets.get("GOOGLE_PLACES_API_KEY") == SENTINEL

        listing = app.handle("GET", "/api/credentials").payload
        google = next(p for p in listing["providers"] if p["key"] == "google_places")
        assert google["secret"]["configured"] is True

    def test_delete_with_a_provider_key_removes_the_value(self, app: ApiApp, service, monkeypatch):
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        app.handle("PUT", "/api/credentials/google_places", body={"value": SENTINEL})
        response = app.handle("DELETE", "/api/credentials/google_places")
        assert response.status == 200
        assert response.payload == {"name": "GOOGLE_PLACES_API_KEY", "removed": True}
        assert service.secrets.get("GOOGLE_PLACES_API_KEY") is None

    def test_put_with_an_unknown_name_is_a_bad_request(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/credentials/NOT_A_REAL_CREDENTIAL", body={"value": SENTINEL}
        )
        assert response.status == 400

    def test_delete_with_an_unknown_name_is_a_bad_request(self, app: ApiApp):
        assert app.handle("DELETE", "/api/credentials/NOT_A_REAL_CREDENTIAL").status == 400

    def test_no_unexpected_name_ever_reaches_the_store(self, app: ApiApp, service):
        app.handle("PUT", "/api/credentials/NOT_A_REAL_CREDENTIAL", body={"value": SENTINEL})
        assert service.secrets._load() == {}