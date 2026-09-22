"""Dashboard JSON API.

A deliberately small router over the standard library. There is no web
framework dependency because the project is stdlib-first and the API surface is
a dozen endpoints over one service object.

Security properties enforced here, not left to callers:

* **No endpoint returns a secret.** Credential endpoints return masked previews
  only. The service is the only thing that touches a raw value.
* **Errors are scrubbed.** Every error message passes through
  :meth:`DashboardService.redact` before it is returned, so an exception that
  happens to quote a credential cannot leak it through a 500.
* **Input is validated before it reaches the service.** A bad field is a 400,
  not a traceback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from lead_finder_agent import __version__
from lead_finder_agent.dashboard.credentials import resolve_secret_name
from lead_finder_agent.dashboard.service import DashboardService
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.api")

#: Caps on request bodies and query strings. A dashboard form is small; a huge
#: body is a mistake or an attack, and either way it should not be buffered.
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_LENGTH = 512


class ApiError(Exception):
    """An error with an HTTP status. Raised by handlers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def bad_request(message: str) -> ApiError:
    return ApiError(400, message)


def not_found(message: str) -> ApiError:
    return ApiError(404, message)


@dataclass
class Response:
    status: int
    payload: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "payload": self.payload}


class Router:
    """Method + path-pattern routing, with ``{name}`` path parameters."""

    def __init__(self) -> None:
        self._routes: List[Tuple[str, re.Pattern, Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, handler))

    def match(self, method: str, path: str):
        allowed: List[str] = []
        for route_method, regex, handler in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method != method.upper():
                allowed.append(route_method)
                continue
            return handler, found.groupdict()
        if allowed:
            raise ApiError(405, f"Method {method} not allowed. Allowed: {', '.join(sorted(set(allowed)))}")
        return None, None

    def routes(self) -> List[str]:
        return sorted({regex.pattern for _, regex, _ in self._routes})


def _int_arg(params: Dict[str, List[str]], name: str, default: Optional[int] = None) -> Optional[int]:
    raw = (params.get(name) or [None])[0]
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise bad_request(f"{name} must be an integer") from None


def _bool_arg(params: Dict[str, List[str]], name: str, default: bool = False) -> bool:
    raw = (params.get(name) or [None])[0]
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _str_arg(params: Dict[str, List[str]], name: str) -> Optional[str]:
    raw = (params.get(name) or [None])[0]
    if raw in (None, ""):
        return None
    if len(str(raw)) > MAX_QUERY_LENGTH:
        raise bad_request(f"{name} is too long")
    return str(raw)


def _list_arg(params: Dict[str, List[str]], name: str) -> Optional[List[str]]:
    raw = _str_arg(params, name)
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_router(service: DashboardService) -> Router:
    """Wire every endpoint to the service."""
    router = Router()

    # -- meta --------------------------------------------------------------

    def api_index(*, body, params, **_) -> Response:
        return Response(
            200,
            {
                "name": "Lead Finder Agent dashboard",
                "version": __version__,
                "endpoints": [
                    "GET  /api/health",
                    "GET  /api/overview",
                    "GET  /api/agents",
                    "GET  /api/agents/{name}",
                    "PUT  /api/agents/{name}",
                    "POST /api/agents/{name}/reset",
                    "POST /api/agents/{name}/run",
                    "GET  /api/credentials",
                    "PUT  /api/credentials/{name}",
                    "DELETE /api/credentials/{name}",
                    "GET  /api/leads",
                    "GET  /api/leads/facets",
                    "GET  /api/leads/{lead_id}",
                    "GET  /api/runs",
                    "GET  /api/runs/{job_id}",
                    "POST /api/runs/search",
                    "POST /api/runs/analyze",
                    "GET  /api/settings",
                    "PUT  /api/settings",
                ],
            },
        )

    router.add("GET", "/api", api_index)

    def health(*, body, params, **_) -> Response:
        return Response(200, service.health())

    router.add("GET", "/api/health", health)

    def overview(*, body, params, **_) -> Response:
        return Response(200, service.overview())

    router.add("GET", "/api/overview", overview)

    # -- agents ------------------------------------------------------------

    def list_agents(*, body, params, **_) -> Response:
        agents = service.list_agents()
        return Response(200, {"agents": agents, "total": len(agents)})

    router.add("GET", "/api/agents", list_agents)

    def get_agent(*, body, params, name: str) -> Response:
        try:
            return Response(200, service.get_agent(name))
        except KeyError as exc:
            raise not_found(str(exc)) from None

    router.add("GET", "/api/agents/{name}", get_agent)

    def update_agent(*, body, params, name: str) -> Response:
        if not isinstance(body, dict):
            raise bad_request("A JSON object is required")
        allowed = {"enabled", "instructions", "settings", "allowed_tools", "notes"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise bad_request(f"Unknown field(s): {', '.join(unknown)}")
        try:
            return Response(200, service.update_agent(name, body))
        except KeyError as exc:
            raise not_found(str(exc)) from None
        except ValueError as exc:
            raise bad_request(str(exc)) from None

    router.add("PUT", "/api/agents/{name}", update_agent)

    def reset_agent(*, body, params, name: str) -> Response:
        try:
            return Response(200, service.reset_agent(name))
        except KeyError as exc:
            raise not_found(str(exc)) from None

    router.add("POST", "/api/agents/{name}/reset", reset_agent)

    def run_agent(*, body, params, name: str) -> Response:
        payload = body if isinstance(body, dict) else {}
        try:
            if name == "lead_finder":
                outcome = service.start_search(
                    city=payload.get("city"),
                    country=payload.get("country"),
                    business_type=payload.get("business_type"),
                    providers=payload.get("providers"),
                    limit=payload.get("limit"),
                    check_websites=bool(payload.get("check_websites", True)),
                    max_checks=payload.get("max_checks"),
                )
            elif name == "website_analyzer":
                outcome = service.start_analysis(
                    limit=payload.get("limit"),
                    lead_id=payload.get("lead_id"),
                    store=bool(payload.get("store", False)),
                    min_severity=payload.get("min_severity"),
                )
            else:
                raise bad_request(f"Agent {name!r} cannot be run from the dashboard")
        except PermissionError as exc:
            raise ApiError(409, str(exc)) from None
        return Response(202, outcome)

    router.add("POST", "/api/agents/{name}/run", run_agent)

    # -- credentials -------------------------------------------------------

    def list_credentials(*, body, params, **_) -> Response:
        entries = service.list_credentials()
        return Response(200, {"providers": entries, "total": len(entries)})

    router.add("GET", "/api/credentials", list_credentials)

    def set_credential(*, body, params, name: str) -> Response:
        if not isinstance(body, dict) or "value" not in body:
            raise bad_request("A JSON object with a 'value' field is required")
        value = body.get("value")
        if not isinstance(value, str) or not value.strip():
            raise bad_request("'value' must be a non-empty string")
        try:
            result = service.set_credential(name, value)
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return Response(200, result)

    router.add("PUT", "/api/credentials/{name}", set_credential)

    def delete_credential(*, body, params, name: str) -> Response:
        try:
            removed = service.delete_credential(name)
            resolved = resolve_secret_name(name)
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return Response(200, {"name": resolved, "removed": removed})

    router.add("DELETE", "/api/credentials/{name}", delete_credential)

    # -- leads -------------------------------------------------------------

    def list_leads(*, body, params, **_) -> Response:
        status = _str_arg(params, "website_status")
        if status:
            from lead_finder_agent.models import WebsiteStatus

            if status not in {str(s) for s in WebsiteStatus}:
                raise bad_request(
                    f"website_status must be one of: {', '.join(str(s) for s in WebsiteStatus)}"
                )
        order_by = _str_arg(params, "order_by") or "lead_score"
        from lead_finder_agent.storage.base import LeadFilter

        if order_by not in LeadFilter.ALLOWED_ORDER:
            raise bad_request(
                f"order_by must be one of: {', '.join(LeadFilter.ALLOWED_ORDER)}"
            )
        result = service.search_leads(
            city=_str_arg(params, "city"),
            country=_str_arg(params, "country"),
            business_type=_str_arg(params, "business_type"),
            source=_str_arg(params, "source"),
            priority=_str_arg(params, "priority"),
            website_status=status,
            min_score=_int_arg(params, "min_score"),
            max_score=_int_arg(params, "max_score"),
            text=_str_arg(params, "q"),
            order_by=order_by,
            descending=not _bool_arg(params, "ascending", False),
            limit=_int_arg(params, "limit", 25) or 25,
            offset=_int_arg(params, "offset", 0) or 0,
        )
        return Response(200, result)

    router.add("GET", "/api/leads", list_leads)

    def lead_facets(*, body, params, **_) -> Response:
        return Response(200, service.lead_facets())

    router.add("GET", "/api/leads/facets", lead_facets)

    def get_lead(*, body, params, lead_id: str) -> Response:
        try:
            return Response(200, service.get_lead(lead_id))
        except KeyError as exc:
            raise not_found(str(exc)) from None

    router.add("GET", "/api/leads/{lead_id}", get_lead)

    # -- runs --------------------------------------------------------------

    def list_runs(*, body, params, **_) -> Response:
        return Response(
            200,
            service.list_runs(
                limit=_int_arg(params, "limit", 25) or 25,
                status=_str_arg(params, "status"),
            ),
        )

    router.add("GET", "/api/runs", list_runs)

    def get_run(*, body, params, job_id: str) -> Response:
        try:
            return Response(200, service.get_run(job_id))
        except KeyError as exc:
            raise not_found(str(exc)) from None

    router.add("GET", "/api/runs/{job_id}", get_run)

    def start_search(*, body, params, **_) -> Response:
        payload = body if isinstance(body, dict) else {}
        try:
            outcome = service.start_search(
                city=payload.get("city"),
                country=payload.get("country"),
                business_type=payload.get("business_type"),
                providers=payload.get("providers"),
                limit=payload.get("limit"),
                check_websites=bool(payload.get("check_websites", True)),
                max_checks=payload.get("max_checks"),
            )
        except PermissionError as exc:
            raise ApiError(409, str(exc)) from None
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return Response(202, outcome)

    router.add("POST", "/api/runs/search", start_search)

    def start_analyze(*, body, params, **_) -> Response:
        payload = body if isinstance(body, dict) else {}
        try:
            outcome = service.start_analysis(
                limit=payload.get("limit"),
                lead_id=payload.get("lead_id"),
                store=bool(payload.get("store", False)),
                min_severity=payload.get("min_severity"),
            )
        except PermissionError as exc:
            raise ApiError(409, str(exc)) from None
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return Response(202, outcome)

    router.add("POST", "/api/runs/analyze", start_analyze)

    # -- settings ----------------------------------------------------------

    def list_settings(*, body, params, **_) -> Response:
        return Response(200, {"settings": service.list_settings()})

    router.add("GET", "/api/settings", list_settings)

    def update_settings(*, body, params, **_) -> Response:
        if not isinstance(body, dict):
            raise bad_request("A JSON object is required")
        # Accept either {"values": {...}} or a bare mapping.
        values = body.get("values") if isinstance(body.get("values"), dict) else body
        try:
            return Response(200, {"settings": service.update_settings(values)})
        except ValueError as exc:
            raise bad_request(str(exc)) from None

    router.add("PUT", "/api/settings", update_settings)

    return router


class ApiApp:
    """Turns an HTTP request into a :class:`Response`."""

    def __init__(self, service: DashboardService) -> None:
        self.service = service
        self.router = build_router(service)

    def handle(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, List[str]]] = None,
        body: Any = None,
    ) -> Response:
        """Dispatch one request. Never raises: failures become a Response."""
        params = params or {}
        try:
            handler, groups = self.router.match(method, path)
            if handler is None:
                return Response(
                    404,
                    {"error": f"No such endpoint: {method} {path}"},
                )
            kwargs = dict(groups or {})
            return handler(body=body, params=params, **kwargs)
        except ApiError as exc:
            return Response(exc.status, {"error": self.service.redact(exc.message)})
        except Exception as exc:  # noqa: BLE001 - the API must never traceback
            log.error("Unhandled API error on %s %s: %s", method, path, exc)
            return Response(
                500,
                {"error": self.service.redact(str(exc)) or "Internal error"},
            )


def decode_body(raw: bytes, content_type: str) -> Any:
    """Parse a request body, or raise :class:`ApiError`."""
    if not raw:
        return None
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "Request body is too large")
    text = raw.decode("utf-8", errors="replace")
    if "json" not in (content_type or "").lower():
        raise bad_request("Content-Type must be application/json")
    try:
        return json.loads(text or "null")
    except ValueError as exc:
        raise bad_request(f"Invalid JSON body: {exc}") from None


__all__ = ["ApiApp", "ApiError", "Response", "Router", "build_router", "decode_body", "MAX_BODY_BYTES"]