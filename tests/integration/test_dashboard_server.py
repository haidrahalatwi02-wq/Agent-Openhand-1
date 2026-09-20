"""Dashboard server tests: real HTTP over loopback.

These drive an actual :class:`~http.server.ThreadingHTTPServer` on an ephemeral
port, so the request parsing, static-file serving, JSON encoding and security
headers are exercised as a client would see them rather than through the router
directly. No test contacts the network beyond loopback.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lead_finder_agent.config.settings import Settings
from lead_finder_agent.dashboard.server import create_server
from lead_finder_agent.dashboard.service import DashboardService


@pytest.fixture
def live_server(tmp_path: Path):
    """A running dashboard on 127.0.0.1 with an ephemeral port."""
    settings = Settings(
        db_path=tmp_path / "leads.db",
        default_city="Aden",
        default_country="Yemen",
        providers=["sample"],
    )
    service = DashboardService(settings=settings, data_dir=tmp_path / "dash")
    server = create_server(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, service
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=5)


def http(base: str, path: str, method: str = "GET", body=None, headers=None):
    """Make a request, returning (status, headers, text)."""
    request = urllib.request.Request(base + path, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=15) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


class TestStaticFiles:
    def test_root_serves_the_dashboard_shell(self, live_server):
        base, _ = live_server
        status, headers, body = http(base, "/")
        assert status == 200
        assert "Lead Finder" in body
        assert "text/html" in headers.get("Content-Type", "")

    def test_stylesheet_is_served(self, live_server):
        base, _ = live_server
        status, headers, body = http(base, "/style.css")
        assert status == 200
        assert "text/css" in headers.get("Content-Type", "")
        assert "--accent" in body

    def test_javascript_is_served(self, live_server):
        base, _ = live_server
        status, headers, body = http(base, "/app.js")
        assert status == 200
        assert "javascript" in headers.get("Content-Type", "")
        assert "renderOverview" in body

    def test_missing_asset_is_404(self, live_server):
        base, _ = live_server
        assert http(base, "/nope.js")[0] == 404

    def test_path_traversal_is_blocked(self, live_server):
        base, _ = live_server
        for path in ("/../pyproject.toml", "/../../etc/passwd", "/..%2fpyproject.toml"):
            status, _, body = http(base, path)
            assert status in (403, 404), path
            assert "dependencies" not in body

    def test_unknown_page_falls_back_to_the_shell(self, live_server):
        base, _ = live_server
        status, headers, body = http(base, "/leads")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")


class TestSecurityHeaders:
    def test_responses_are_not_cached(self, live_server):
        base, _ = live_server
        _, headers, _ = http(base, "/api/credentials")
        assert headers.get("Cache-Control") == "no-store"

    def test_content_type_is_not_sniffed(self, live_server):
        base, _ = live_server
        _, headers, _ = http(base, "/api/overview")
        assert headers.get("X-Content-Type-Options") == "nosniff"

    def test_referrer_is_not_leaked(self, live_server):
        base, _ = live_server
        _, headers, _ = http(base, "/")
        assert headers.get("Referrer-Policy") == "no-referrer"


class TestApiOverHttp:
    def test_health(self, live_server):
        base, _ = live_server
        status, headers, body = http(base, "/api/health")
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        assert json.loads(body)["ok"] is True

    def test_overview(self, live_server):
        base, _ = live_server
        assert http(base, "/api/overview")[0] == 200

    def test_unknown_endpoint_is_404(self, live_server):
        base, _ = live_server
        status, _, body = http(base, "/api/unknown")
        assert status == 404
        assert "error" in json.loads(body)

    def test_put_requires_json_content_type(self, live_server):
        base, _ = live_server
        request = urllib.request.Request(
            base + "/api/settings", method="PUT", data=b'{"values":{}}'
        )
        request.add_header("Content-Type", "text/plain")
        try:
            urllib.request.urlopen(request, timeout=15)
            raise AssertionError("expected a 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_invalid_json_body_is_400(self, live_server):
        base, _ = live_server
        request = urllib.request.Request(
            base + "/api/settings", method="PUT", data=b"{ broken"
        )
        request.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(request, timeout=15)
            raise AssertionError("expected a 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_oversized_body_is_rejected(self, live_server):
        base, _ = live_server
        payload = json.dumps({"values": {"default_city": "x" * 200_000}}).encode()
        request = urllib.request.Request(base + "/api/settings", method="PUT", data=payload)
        request.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(request, timeout=15)
            raise AssertionError("expected a rejection")
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 413)

    def test_credentials_never_return_a_secret_over_http(self, live_server, monkeypatch):
        base, _ = live_server
        sentinel = "http-sentinel-value-4b7d2e"
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", sentinel)
        status, _, body = http(base, "/api/credentials")
        assert status == 200
        assert sentinel not in body

    def test_stored_credential_is_not_returned_over_http(self, live_server, monkeypatch):
        base, _ = live_server
        sentinel = "http-store-sentinel-1a2b3c"
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        status, _, body = http(
            base, "/api/credentials/SMTP_PASSWORD", "PUT", body={"value": sentinel}
        )
        assert status == 200
        assert sentinel not in body
        # And subsequent reads still do not expose it.
        assert sentinel not in http(base, "/api/credentials")[2]


class TestConcurrentRequests:
    def test_server_answers_while_a_run_is_in_flight(self, live_server):
        """A background run must not block the request that polls it."""
        base, service = live_server
        status, _, body = http(
            base,
            "/api/runs/search",
            "POST",
            body={"providers": ["sample"], "limit": 3, "check_websites": False},
        )
        assert status == 202
        job_id = json.loads(body)["id"]

        # Poll concurrently: the health endpoint must stay answerable.
        assert http(base, "/api/health")[0] == 200
        service.runner.join(job_id, timeout=60)

        status, _, body = http(base, f"/api/runs/{job_id}")
        assert status == 200
        assert json.loads(body)["status"] == "completed"

    def test_parallel_reads_are_consistent(self, live_server):
        base, _ = live_server
        results = []
        lock = threading.Lock()

        def worker():
            status, _, body = http(base, "/api/overview")
            with lock:
                results.append((status, body))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 8
        assert all(status == 200 for status, _ in results)


class TestServerConfig:
    def test_defaults_bind_to_loopback(self):
        from lead_finder_agent.dashboard.server import DEFAULT_HOST, DEFAULT_PORT

        assert DEFAULT_HOST == "127.0.0.1"
        assert isinstance(DEFAULT_PORT, int)

    def test_port_zero_selects_an_ephemeral_port(self, tmp_path):
        settings = Settings(db_path=tmp_path / "x.db", providers=["sample"])
        service = DashboardService(settings=settings, data_dir=tmp_path / "dash")
        server = create_server(service, port=0)
        try:
            assert server.server_address[1] > 0
        finally:
            server.server_close()
            service.close()