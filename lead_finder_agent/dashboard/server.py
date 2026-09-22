"""The dashboard HTTP server.

Built on :mod:`http.server` so the project keeps its single runtime dependency.
The handler is deliberately thin: it parses a request, hands it to
:class:`~lead_finder_agent.dashboard.api.ApiApp`, and writes the response.

Security-relevant choices, all deliberate:

* **Binds to loopback by default.** The dashboard can trigger network work and
  holds masked credential state, so it is not exposed to the network unless a
  caller explicitly asks for another host.
* **Static files are served from one directory, by name.** The path is resolved
  and then checked to be inside that directory, so ``../`` cannot escape it.
* **No secret is ever written to a response body by the server itself.** JSON
  comes from the API layer, which returns masked state only.
* **Responses are not cached.** A cached credentials response could outlive a
  revocation.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from lead_finder_agent.dashboard.api import ApiApp, ApiError, decode_body
from lead_finder_agent.dashboard.service import DashboardService
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Default host. Loopback only: the dashboard is a local control surface.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class DashboardServer(ThreadingHTTPServer):
    """Threaded server carrying the API app.

    ``ThreadingHTTPServer`` matters here: a search run is dispatched to a
    background thread, but polling its status must still be answerable while it
    runs.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: ApiApp) -> None:
        super().__init__(address, DashboardRequestHandler)
        self.app = app


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Routes ``/api/*`` to the app and everything else to static files."""

    server_version = "LeadFinderDashboard/0.1"
    #: Keep the default (HTTP/1.0) so a response is closed after each request;
    #: the dashboard makes small, independent requests.
    protocol_version = "HTTP/1.0"

    # -- helpers -----------------------------------------------------------

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A cached credentials response could outlive a revocation.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._write(status, body, "application/json; charset=utf-8")

    def _log_request(self) -> None:
        # BaseHTTPRequestHandler logs to stderr with the raw path. Query strings
        # could carry a filter that is not secret, but the path is enough, and
        # suppressing the default line avoids double logging.
        return

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "/")
        path = path.rstrip("/") or "/"
        params: Dict[str, List[str]] = parse_qs(parsed.query, keep_blank_values=False)

        if path == "/api" or path.startswith("/api/"):
            self._handle_api(method, path, params)
            return
        if method in ("GET", "HEAD"):
            self._handle_static(path)
            return
        self._write_json(405, {"error": f"Method {method} not allowed here"})

    def _handle_api(self, method: str, path: str, params: Dict[str, List[str]]) -> None:
        body: Any = None
        if method in ("PUT", "POST"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._write_json(400, {"error": "Invalid Content-Length"})
                return
            if length < 0:
                self._write_json(400, {"error": "Invalid Content-Length"})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = decode_body(raw, self.headers.get("Content-Type") or "")
            except ApiError as exc:
                self._write_json(exc.status, {"error": str(exc.message)})
                return
            except Exception as exc:  # noqa: BLE001
                self._write_json(400, {"error": self.server.app.service.redact(str(exc))})
                return

        response = self.server.app.handle(method, path, params=params, body=body)
        self._write_json(response.status, response.payload)

    def _handle_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        # Resolve first, then confirm containment: this is what stops "../".
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            self._write_json(403, {"error": "Forbidden"})
            return
        if not target.is_file():
            # A single-page dashboard: unknown non-API paths fall back to the
            # shell so client-side navigation works on a hard refresh.
            fallback = STATIC_DIR / "index.html"
            if fallback.is_file() and "." not in Path(relative).name:
                target = fallback
            else:
                self._write(404, b"Not found", "text/plain; charset=utf-8")
                return
        try:
            content = target.read_bytes()
        except OSError as exc:
            self._write(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        guessed, _ = mimetypes.guess_type(str(target))
        self._write(200, content, guessed or "application/octet-stream")


def create_server(
    service: Optional[DashboardService] = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> DashboardServer:
    """Build a server bound to ``host:port``. Port 0 picks a free port."""
    service = service or DashboardService()
    app = ApiApp(service)
    server = DashboardServer((host, port), app)
    return server


def serve(
    service: Optional[DashboardService] = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    """Run the dashboard until interrupted."""
    server = create_server(service, host=host, port=port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}/"
    log.info("Dashboard listening on %s", url)
    print(f"Lead Finder dashboard running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, _open_browser, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.shutdown()
        server.server_close()
        service = getattr(server.app, "service", None)
        if service is not None:
            service.close()


def _open_browser(url: str) -> None:  # pragma: no cover - interactive
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "DashboardServer",
    "DashboardRequestHandler",
    "create_server",
    "serve",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "STATIC_DIR",
]