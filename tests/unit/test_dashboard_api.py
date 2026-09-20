"""Dashboard API and service tests.

Everything here runs offline: the Lead Finder is driven with the bundled
``sample`` provider and website checking is disabled, so no test touches the
network. Where a run is started it is joined before the assertions, so the tests
are deterministic rather than timing-dependent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lead_finder_agent.config.settings import Settings
from lead_finder_agent.dashboard.api import ApiApp, Router
from lead_finder_agent.dashboard.service import DashboardService
from lead_finder_agent.dashboard.settings_store import editable_names
from lead_finder_agent.models import Lead, WebsiteStatus


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings: sample provider only, its own database."""
    return Settings(
        db_path=tmp_path / "leads.db",
        default_city="Aden",
        default_country="Yemen",
        providers=["sample"],
    )


@pytest.fixture
def service(settings: Settings, tmp_path: Path) -> DashboardService:
    svc = DashboardService(settings=settings, data_dir=tmp_path / "dash")
    yield svc
    svc.close()


@pytest.fixture
def app(service: DashboardService) -> ApiApp:
    return ApiApp(service)


@pytest.fixture
def seeded(service: DashboardService) -> DashboardService:
    """Store a handful of leads directly, for listing/filtering tests."""
    repository = service._read_context().resolve_repository()
    repository.add_many(
        [
            Lead(
                business_name="Aden Traders",
                business_type="shop",
                city="Aden",
                country="Yemen",
                source="sample",
                website_status=WebsiteStatus.NOT_FOUND,
                lead_score=88,
                priority="hot",
                phone="+967 71 111 2222",
            ),
            Lead(
                business_name="Modern Electronics",
                business_type="electronics",
                city="Aden",
                country="Yemen",
                source="osm",
                website_url="https://modern.example",
                website_status=WebsiteStatus.EXISTS,
                lead_score=64,
                priority="warm",
            ),
            Lead(
                business_name="Sanaa Bakery",
                business_type="bakery",
                city="Sanaa",
                country="Yemen",
                source="osm",
                website_status=WebsiteStatus.UNKNOWN,
                lead_score=12,
                priority="cold",
            ),
        ]
    )
    return service


def run_and_wait(service: DashboardService, job: dict, timeout: float = 60.0):
    service.runner.join(job["id"], timeout=timeout)
    return service.get_run(job["id"])


# --------------------------------------------------------------------------- #
# API basics
# --------------------------------------------------------------------------- #


class TestApiBasics:
    def test_index_lists_endpoints(self, app: ApiApp):
        response = app.handle("GET", "/api")
        assert response.status == 200
        assert response.payload["endpoints"]

    def test_unknown_endpoint_is_404(self, app: ApiApp):
        response = app.handle("GET", "/api/does-not-exist")
        assert response.status == 404
        assert "error" in response.payload

    def test_wrong_method_is_405(self, app: ApiApp):
        assert app.handle("DELETE", "/api/overview").status == 405

    def test_router_reports_allowed_methods(self):
        router = Router()
        router.add("GET", "/x", lambda **_: None)
        with pytest.raises(Exception) as exc:
            router.match("POST", "/x")
        assert "GET" in str(exc.value)

    def test_every_route_is_reachable_by_its_method(self, app: ApiApp):
        """A route added without a handler would 500 instead of routing."""
        for method, path in (
            ("GET", "/api"),
            ("GET", "/api/health"),
            ("GET", "/api/overview"),
            ("GET", "/api/agents"),
            ("GET", "/api/agents/lead_finder"),
            ("GET", "/api/credentials"),
            ("GET", "/api/leads"),
            ("GET", "/api/leads/facets"),
            ("GET", "/api/runs"),
            ("GET", "/api/settings"),
        ):
            assert app.handle(method, path).status == 200, f"{method} {path}"


# --------------------------------------------------------------------------- #
# Health and overview
# --------------------------------------------------------------------------- #


class TestHealthAndOverview:
    def test_health_reports_database_ok(self, app: ApiApp):
        payload = app.handle("GET", "/api/health").payload
        database = next(c for c in payload["checks"] if c["name"] == "database")
        assert database["ok"] is True

    def test_health_reports_provider_availability(self, app: ApiApp):
        payload = app.handle("GET", "/api/health").payload
        assert "sample" in payload["providers"]["available"]

    def test_health_includes_no_secret(self, app: ApiApp, monkeypatch):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "health-secret-value-xyz")
        payload = json.dumps(app.handle("GET", "/api/health").payload)
        assert "health-secret-value-xyz" not in payload

    def test_overview_has_all_required_sections(self, app: ApiApp):
        payload = app.handle("GET", "/api/overview").payload
        for key in (
            "leads",
            "agents",
            "runs",
            "recent_runs",
            "recent_errors",
            "health",
        ):
            assert key in payload, key

    def test_overview_counts_leads_by_priority_and_status(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/overview").payload
        assert payload["leads"]["total"] == 3
        assert payload["leads"]["by_priority"].get("hot") == 1
        assert payload["leads"]["by_website_status"].get("website_exists") == 1

    def test_overview_score_bands_are_partitioned(self, app: ApiApp, seeded):
        bands = app.handle("GET", "/api/overview").payload["leads"]["score_bands"]
        assert bands["hot"] == 1  # 88
        assert bands["warm"] == 1  # 64
        assert bands["cold"] == 1  # 12

    def test_overview_average_score(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/overview").payload
        assert payload["leads"]["average_score"] == pytest.approx(54.7, abs=0.1)

    def test_overview_lists_agents_from_the_manager(self, app: ApiApp):
        names = {a["name"] for a in app.handle("GET", "/api/overview").payload["agents"]}
        assert {"lead_finder", "website_analyzer"} <= names

    def test_overview_on_empty_database_is_valid(self, app: ApiApp):
        payload = app.handle("GET", "/api/overview").payload
        assert payload["leads"]["total"] == 0
        assert payload["leads"]["average_score"] == 0


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


class TestAgents:
    def test_lists_every_registered_agent(self, app: ApiApp):
        payload = app.handle("GET", "/api/agents").payload
        names = [a["name"] for a in payload["agents"]]
        assert names == ["lead_finder", "website_analyzer"]
        assert payload["total"] == 2

    def test_agent_entry_exposes_the_configuration_surface(self, app: ApiApp):
        agent = app.handle("GET", "/api/agents/lead_finder").payload
        for key in (
            "name",
            "description",
            "enabled",
            "instructions",
            "settings",
            "allowed_tools",
            "available_tools",
            "settings_schema",
        ):
            assert key in agent, key

    def test_agent_comes_from_the_manager_not_a_copy(self, app: ApiApp, service):
        from lead_finder_agent.core.manager import AgentManager

        listed = {a["name"] for a in app.handle("GET", "/api/agents").payload["agents"]}
        assert listed == set(service.manager().names())
        assert isinstance(service.manager(), AgentManager)

    def test_unknown_agent_is_404(self, app: ApiApp):
        assert app.handle("GET", "/api/agents/nope").status == 404

    def test_disable_and_enable_round_trip(self, app: ApiApp):
        response = app.handle("PUT", "/api/agents/website_analyzer", body={"enabled": False})
        assert response.status == 200
        assert response.payload["enabled"] is False
        assert app.handle("GET", "/api/agents/website_analyzer").payload["enabled"] is False

        app.handle("PUT", "/api/agents/website_analyzer", body={"enabled": True})
        assert app.handle("GET", "/api/agents/website_analyzer").payload["enabled"] is True

    def test_instructions_are_saved(self, app: ApiApp):
        app.handle(
            "PUT",
            "/api/agents/lead_finder",
            body={"instructions": "Find businesses without a website."},
        )
        agent = app.handle("GET", "/api/agents/lead_finder").payload
        assert agent["instructions"] == "Find businesses without a website."

    def test_settings_are_saved(self, app: ApiApp):
        app.handle("PUT", "/api/agents/lead_finder", body={"settings": {"max_checks": 5}})
        assert app.handle("GET", "/api/agents/lead_finder").payload["settings"]["max_checks"] == 5

    def test_allowed_tools_are_saved(self, app: ApiApp):
        app.handle("PUT", "/api/agents/lead_finder", body={"allowed_tools": ["storage"]})
        assert app.handle("GET", "/api/agents/lead_finder").payload["allowed_tools"] == ["storage"]

    def test_reset_restores_defaults(self, app: ApiApp):
        app.handle("PUT", "/api/agents/lead_finder", body={"instructions": "custom", "enabled": False})
        response = app.handle("POST", "/api/agents/lead_finder/reset")
        assert response.status == 200
        assert response.payload["enabled"] is True
        assert response.payload["instructions"] != "custom"

    def test_unknown_field_is_rejected(self, app: ApiApp):
        response = app.handle("PUT", "/api/agents/lead_finder", body={"bogus": 1})
        assert response.status == 400

    def test_credentials_are_rejected_in_agent_settings(self, app: ApiApp):
        for key in ("api_key", "secret_token", "password", "credential_id"):
            response = app.handle(
                "PUT", "/api/agents/lead_finder", body={"settings": {key: "value"}}
            )
            assert response.status == 400, key

    def test_agent_payload_declares_no_credentials(self, app: ApiApp):
        agent = app.handle("GET", "/api/agents/lead_finder").payload
        assert agent["has_credentials"] is False


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


class TestCredentialEndpoints:
    def test_every_channel_is_represented(self, app: ApiApp):
        payload = app.handle("GET", "/api/credentials").payload
        channels = {p["channel"] for p in payload["providers"]}
        assert {"search", "llm", "email", "messaging"} <= channels

    def test_search_providers_are_present(self, app: ApiApp):
        payload = app.handle("GET", "/api/credentials").payload
        keys = {p["key"] for p in payload["providers"]}
        assert {"osm", "sample", "google_places"} <= keys

    def test_osm_reports_no_key_required(self, app: ApiApp):
        payload = app.handle("GET", "/api/credentials").payload
        osm = next(p for p in payload["providers"] if p["key"] == "osm")
        assert osm["requires_secret"] is False
        assert osm["secret_env"] is None

    def test_keyless_provider_has_no_secret_state(self, app: ApiApp):
        """A keyless provider must not claim a stored value.

        This used to report ``configured: True`` with an empty preview, which the
        UI rendered as a masked secret that does not exist.
        """
        payload = app.handle("GET", "/api/credentials").payload
        for key in ("osm", "sample"):
            entry = next(p for p in payload["providers"] if p["key"] == key)
            assert entry["secret"]["configured"] is False
            assert entry["secret"]["preview"] == ""

    def test_unconfigured_secret_reports_false(self, app: ApiApp, monkeypatch):
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        payload = app.handle("GET", "/api/credentials").payload
        google = next(p for p in payload["providers"] if p["key"] == "google_places")
        assert google["secret"]["configured"] is False

    def test_environment_secret_reports_configured(self, app: ApiApp, monkeypatch):
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "env-key-value-123456")
        payload = app.handle("GET", "/api/credentials").payload
        google = next(p for p in payload["providers"] if p["key"] == "google_places")
        assert google["secret"]["configured"] is True
        assert google["secret"]["source"] == "environment"
        assert google["secret"]["preview"].endswith("3456")


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #


class TestLeads:
    def test_lists_all_leads(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads").payload
        assert payload["total"] == 3
        assert len(payload["leads"]) == 3

    def test_ordered_by_score_descending_by_default(self, app: ApiApp, seeded):
        scores = [lead["lead_score"] for lead in app.handle("GET", "/api/leads").payload["leads"]]
        assert scores == sorted(scores, reverse=True)

    def test_ascending_order(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"ascending": ["true"]}).payload
        scores = [lead["lead_score"] for lead in payload["leads"]]
        assert scores == sorted(scores)

    def test_filter_by_city(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"city": ["Aden"]}).payload
        assert payload["total"] == 2

    def test_filter_by_priority(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"priority": ["hot"]}).payload
        assert payload["total"] == 1
        assert payload["leads"][0]["business_name"] == "Aden Traders"

    def test_filter_by_website_status(self, app: ApiApp, seeded):
        payload = app.handle(
            "GET", "/api/leads", params={"website_status": ["website_not_found"]}
        ).payload
        assert payload["total"] == 1

    def test_filter_by_min_score(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"min_score": ["60"]}).payload
        assert payload["total"] == 2

    def test_filter_by_source(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"source": ["osm"]}).payload
        assert payload["total"] == 2

    def test_filter_by_business_type(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"business_type": ["bakery"]}).payload
        assert payload["total"] == 1

    def test_text_search_matches_name(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"q": ["modern"]}).payload
        assert payload["total"] == 1
        assert payload["leads"][0]["business_name"] == "Modern Electronics"

    def test_text_search_is_case_insensitive(self, app: ApiApp, seeded):
        payload = app.handle("GET", "/api/leads", params={"q": ["ADEN"]}).payload
        assert payload["total"] == 1

    def test_pagination(self, app: ApiApp, seeded):
        first = app.handle("GET", "/api/leads", params={"limit": ["2"], "offset": ["0"]}).payload
        second = app.handle("GET", "/api/leads", params={"limit": ["2"], "offset": ["2"]}).payload
        assert len(first["leads"]) == 2
        assert len(second["leads"]) == 1
        assert first["total"] == second["total"] == 3
        assert first["leads"][0]["id"] != second["leads"][0]["id"]

    def test_combined_filters(self, app: ApiApp, seeded):
        payload = app.handle(
            "GET", "/api/leads", params={"city": ["Aden"], "min_score": ["70"]}
        ).payload
        assert payload["total"] == 1
        assert payload["leads"][0]["business_name"] == "Aden Traders"

    def test_invalid_website_status_is_400(self, app: ApiApp):
        response = app.handle("GET", "/api/leads", params={"website_status": ["nonsense"]})
        assert response.status == 400

    def test_invalid_order_by_is_400(self, app: ApiApp):
        response = app.handle("GET", "/api/leads", params={"order_by": ["drop table"]})
        assert response.status == 400

    def test_invalid_limit_is_400(self, app: ApiApp):
        assert app.handle("GET", "/api/leads", params={"limit": ["abc"]}).status == 400

    def test_lead_detail_returns_full_record(self, app: ApiApp, seeded):
        lead_id = app.handle("GET", "/api/leads").payload["leads"][0]["id"]
        detail = app.handle("GET", f"/api/leads/{lead_id}").payload
        for key in ("business_name", "lead_score", "score_reason", "website_check", "website_analysis"):
            assert key in detail, key

    def test_lead_detail_includes_contact_fields(self, app: ApiApp, seeded):
        hot = app.handle("GET", "/api/leads", params={"priority": ["hot"]}).payload["leads"][0]
        detail = app.handle("GET", f"/api/leads/{hot['id']}").payload
        assert detail["phone"] == "+967 71 111 2222"
        assert detail["city"] == "Aden"

    def test_unknown_lead_is_404(self, app: ApiApp):
        assert app.handle("GET", "/api/leads/nope").status == 404

    def test_facets_return_real_values(self, app: ApiApp, seeded):
        facets = app.handle("GET", "/api/leads/facets").payload
        assert set(facets["cities"]) == {"Aden", "Sanaa"}
        assert set(facets["sources"]) == {"osm", "sample"}
        assert "website_not_found" in facets["website_statuses"]

    def test_empty_database_lists_nothing(self, app: ApiApp):
        payload = app.handle("GET", "/api/leads").payload
        assert payload["total"] == 0
        assert payload["leads"] == []


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


class TestRuns:
    def test_search_run_completes_and_stores_leads(self, app: ApiApp, service):
        response = app.handle(
            "POST",
            "/api/runs/search",
            body={"providers": ["sample"], "limit": 4, "check_websites": False},
        )
        assert response.status == 202
        finished = run_and_wait(service, response.payload)
        assert finished["status"] == "completed"
        assert finished["metrics"]["stored_count"] == 4
        assert app.handle("GET", "/api/leads").payload["total"] == 4

    def test_run_goes_through_the_agent_manager(self, app: ApiApp, service, monkeypatch):
        """The run must be dispatched by the manager, not by a private path."""
        called = {}
        original = service.build_manager

        def spy():
            manager = original()
            real_run = manager.run

            def wrapped(name, **kwargs):
                called["agent"] = name
                return real_run(name, **kwargs)

            manager.run = wrapped
            return manager

        monkeypatch.setattr(service, "build_manager", spy)
        response = app.handle(
            "POST", "/api/runs/search", body={"providers": ["sample"], "limit": 2, "check_websites": False}
        )
        run_and_wait(service, response.payload)
        assert called.get("agent") == "lead_finder"

    def test_search_run_records_pipeline_metrics(self, app: ApiApp, service):
        response = app.handle(
            "POST",
            "/api/runs/search",
            body={"city": "Aden", "providers": ["sample"], "limit": 3, "check_websites": False},
        )
        metrics = run_and_wait(service, response.payload)["metrics"]
        assert metrics["raw_count"] == 3
        assert metrics["normalized_count"] == 3
        assert metrics["scored_count"] == 3

    def test_disabled_agent_refuses_to_run(self, app: ApiApp):
        app.handle("PUT", "/api/agents/lead_finder", body={"enabled": False})
        response = app.handle("POST", "/api/runs/search", body={"limit": 1})
        assert response.status == 409
        assert "disabled" in response.payload["error"].lower()

    def test_analysis_run_completes(self, app: ApiApp, service, seeded):
        response = app.handle("POST", "/api/runs/analyze", body={"limit": 3})
        assert response.status == 202
        finished = run_and_wait(service, response.payload)
        assert finished["status"] == "completed"
        assert finished["metrics"]["analysed"] == 3

    def test_failed_run_is_recorded_with_an_error(self, app: ApiApp, service, monkeypatch):
        """A job that raises must appear as failed, with its message."""
        def exploding_manager():
            raise RuntimeError("provider blew up")

        monkeypatch.setattr(service, "build_manager", exploding_manager)
        response = app.handle("POST", "/api/runs/search", body={"limit": 1})
        finished = run_and_wait(service, response.payload)
        assert finished["status"] == "failed"
        assert "blew up" in finished["error"]
        # A failed run must still be visible in the history, not dropped.
        assert any(r.id == finished["id"] for r in service.jobs.recent(5))

    def test_run_listing_includes_counts(self, app: ApiApp, service):
        app.handle("POST", "/api/runs/search", body={"providers": ["sample"], "limit": 1, "check_websites": False})
        payload = app.handle("GET", "/api/runs").payload
        assert payload["counts"]["total"] >= 1
        assert any(r["agent"] == "lead_finder" for r in payload["runs"])

    def test_run_can_be_listed_by_status(self, app: ApiApp, service):
        app.handle("POST", "/api/runs/search", body={"providers": ["sample"], "limit": 1, "check_websites": False})
        service.runner.join(service.jobs.recent(1)[0].id, timeout=60)
        payload = app.handle("GET", "/api/runs", params={"status": ["completed"]}).payload
        assert all(r["status"] == "completed" for r in payload["runs"])

    def test_unknown_run_is_404(self, app: ApiApp):
        assert app.handle("GET", "/api/runs/deadbeef").status == 404

    def test_run_detail_exposes_params_and_metrics(self, app: ApiApp, service):
        response = app.handle(
            "POST", "/api/runs/search", body={"city": "Aden", "providers": ["sample"], "limit": 2, "check_websites": False}
        )
        detail = run_and_wait(service, response.payload)
        assert detail["params"]["city"] == "Aden"
        assert detail["metrics"]["raw_count"] == 2

    def test_agent_run_endpoint_dispatches_by_name(self, app: ApiApp, service):
        response = app.handle(
            "POST",
            "/api/agents/lead_finder/run",
            body={"providers": ["sample"], "limit": 2, "check_websites": False},
        )
        assert response.status == 202
        assert run_and_wait(service, response.payload)["status"] == "completed"

    def test_agent_run_endpoint_rejects_an_unrunnable_agent(self, app: ApiApp, service):
        registered = service.manager().names()
        response = app.handle("POST", "/api/agents/not-an-agent/run", body={})
        # Not registered at all -> 400 from the dispatch table.
        assert response.status in (400, 404), registered


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_lists_every_editable_setting(self, app: ApiApp):
        payload = app.handle("GET", "/api/settings").payload
        assert set(payload["settings"]) == set(editable_names())

    def test_settings_report_source(self, app: ApiApp):
        payload = app.handle("GET", "/api/settings").payload
        assert payload["settings"]["default_city"]["source"] == "default"

    def test_update_creates_an_override(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/settings", body={"values": {"default_city": "Sanaa"}}
        )
        assert response.status == 200
        assert response.payload["settings"]["default_city"]["value"] == "Sanaa"
        assert response.payload["settings"]["default_city"]["source"] == "override"

    def test_clearing_a_value_restores_the_default(self, app: ApiApp):
        app.handle("PUT", "/api/settings", body={"values": {"default_city": "Sanaa"}})
        response = app.handle("PUT", "/api/settings", body={"values": {"default_city": None}})
        assert response.payload["settings"]["default_city"]["source"] == "default"
        assert response.payload["settings"]["default_city"]["value"] == "Aden"

    def test_bare_mapping_is_accepted(self, app: ApiApp):
        response = app.handle("PUT", "/api/settings", body={"default_country": "Yemen"})
        assert response.status == 200

    def test_unknown_setting_is_rejected(self, app: ApiApp):
        response = app.handle("PUT", "/api/settings", body={"values": {"evil": "x"}})
        assert response.status == 400

    def test_list_setting_accepts_comma_separated_text(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/settings", body={"values": {"providers": "sample,osm"}}
        )
        assert response.payload["settings"]["providers"]["value"] == ["sample", "osm"]

    def test_int_setting_is_coerced(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/settings", body={"values": {"website_max_redirects": "3"}}
        )
        assert response.payload["settings"]["website_max_redirects"]["value"] == 3

    def test_invalid_int_is_400(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/settings", body={"values": {"website_max_redirects": "many"}}
        )
        assert response.status == 400

    def test_bool_setting_is_coerced(self, app: ApiApp):
        response = app.handle(
            "PUT", "/api/settings", body={"values": {"website_cache_enabled": "false"}}
        )
        assert response.payload["settings"]["website_cache_enabled"]["value"] is False

    def test_enum_setting_rejects_an_unknown_choice(self, app: ApiApp):
        response = app.handle("PUT", "/api/settings", body={"values": {"log_level": "LOUD"}})
        assert response.status == 400

    def test_schema_exposes_no_secret_fields(self):
        from lead_finder_agent.dashboard.settings_store import SCHEMA

        for entry in SCHEMA:
            assert not any(
                hint in entry["name"].lower()
                for hint in ("key", "secret", "token", "password")
            ), entry["name"]

    def test_overrides_apply_to_the_effective_settings(self, service, app):
        app.handle("PUT", "/api/settings", body={"values": {"default_city": "Sanaa"}})
        assert service.effective_settings().default_city == "Sanaa"


# --------------------------------------------------------------------------- #
# Persistence and isolation
# --------------------------------------------------------------------------- #


class TestPersistence:
    def test_agent_config_survives_a_restart(self, settings, tmp_path):
        first = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        first.update_agent("lead_finder", {"instructions": "persisted instructions"})
        first.close()

        second = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        assert second.get_agent("lead_finder")["instructions"] == "persisted instructions"
        second.close()

    def test_settings_survive_a_restart(self, settings, tmp_path):
        first = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        first.update_settings({"default_city": "Sanaa"})
        first.close()

        second = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        assert second.effective_settings().default_city == "Sanaa"
        second.close()

    def test_run_history_survives_a_restart(self, settings, tmp_path):
        first = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        job = first.start_search(providers=["sample"], limit=2, check_websites=False)
        first.runner.join(job["id"], timeout=60)
        first.close()

        second = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        assert second.get_run(job["id"])["status"] == "completed"
        second.close()

    def test_interrupted_run_is_reported_as_failed(self, settings, tmp_path):
        """A run left 'running' by a crash must not stay running forever."""
        data_dir = tmp_path / "dash"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "runs.json").write_text(
            json.dumps(
                [
                    {
                        "id": "abc123",
                        "agent": "lead_finder",
                        "status": "running",
                        "params": {},
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ]
            ),
            encoding="utf-8",
        )
        service = DashboardService(settings=settings, data_dir=data_dir)
        assert service.get_run("abc123")["status"] == "failed"
        service.close()

    def test_corrupt_agent_config_falls_back_to_defaults(self, settings, tmp_path):
        data_dir = tmp_path / "dash"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "agent_config.json").write_text("}{ not json", encoding="utf-8")
        service = DashboardService(settings=settings, data_dir=data_dir)
        assert service.get_agent("lead_finder")["enabled"] is True
        service.close()

    def test_history_is_bounded(self, settings, tmp_path):
        from lead_finder_agent.dashboard.jobs import JobStore

        store = JobStore(tmp_path, limit=3)
        for _ in range(6):
            record = store.start("lead_finder", {})
            store.finish(record.id, status="completed")
        assert store.counts()["total"] == 3


class TestInjectedManager:
    def test_an_injected_manager_is_used_verbatim(self, settings, tmp_path):
        """The dashboard must not build a second registry behind our back."""
        from lead_finder_agent.core.manager import AgentManager

        manager = AgentManager()
        service = DashboardService(settings=settings, data_dir=tmp_path / "dash", manager=manager)
        try:
            assert service.manager() is manager
            # No agents registered on the injected manager -> empty listing.
            assert service.list_agents() == []
        finally:
            service.close()


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


class TestConcurrentRuns:
    def test_a_run_is_not_locked_out_by_concurrent_reads(self, service):
        """A run on a fresh database must not lose a race with reads.

        Creating the repository writes the schema. A run and a read arriving
        together used to collide, and the run failed with "database is locked".
        The reader threads here start before the first run and poll throughout.
        """
        stop = threading.Event()
        read_errors = []

        def reader():
            while not stop.is_set():
                try:
                    service.overview()
                except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                    read_errors.append(repr(exc))
                    return

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers:
            thread.start()
        try:
            for _ in range(5):
                job = service.start_search(providers=["sample"], limit=2, check_websites=False)
                service.runner.join(job["id"], timeout=60)
                final = service.get_run(job["id"])
                assert final["status"] == "completed", final.get("error")
        finally:
            stop.set()
            for thread in readers:
                thread.join(timeout=10)

        assert read_errors == []

    def test_readers_never_observe_a_locked_database(self, service):
        """Parallel readers on a fresh database must all succeed."""
        errors = []
        lock = threading.Lock()

        def read():
            try:
                service.overview()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=read) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []