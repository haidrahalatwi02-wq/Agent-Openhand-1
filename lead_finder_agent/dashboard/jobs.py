"""Run history: recent, running, completed and failed jobs.

Execution history is recorded here rather than inferred from the database,
because a run that failed before storing anything still happened and still
needs to be visible. The store keeps a bounded ring of records so a long-lived
dashboard cannot grow without limit.

A job is a thin wrapper around one call into the existing
:class:`~lead_finder_agent.core.manager.AgentManager`. Nothing about the
pipeline is reimplemented: :class:`JobRunner` invokes the manager and records
what came back.
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("dashboard.jobs")

RUNS_FILENAME = "runs.json"

#: Outcome labels. A run is never silently dropped from the history.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

DEFAULT_HISTORY_LIMIT = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    """One recorded execution of an agent."""

    id: str
    agent: str
    status: str
    params: Dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    summary: str = ""
    #: Counters returned by the run, e.g. raw/stored counts. Plain numbers.
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["is_running"] = self.is_running
        return data


class JobStore:
    """Bounded, thread-safe run history persisted as JSON."""

    def __init__(self, data_dir: Path, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / RUNS_FILENAME
        self.limit = max(1, int(limit))
        self._lock = threading.RLock()
        self._records: Optional[List[JobRecord]] = None

    def _load(self) -> List[JobRecord]:
        with self._lock:
            if self._records is not None:
                return self._records
            records: List[JobRecord] = []
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                    if isinstance(raw, list):
                        for entry in raw:
                            if not isinstance(entry, dict):
                                continue
                            known = {
                                k: v for k, v in entry.items() if k in JobRecord.__dataclass_fields__
                            }
                            known.pop("is_running", None)
                            records.append(JobRecord(**known))
            except (ValueError, OSError, TypeError) as exc:
                log.warning("Could not read run history (%s); starting empty", exc)
                records = []
            # A run interrupted by a restart would otherwise show as "running"
            # forever. Marking it failed is the honest reading.
            for record in records:
                if record.is_running:
                    record.status = STATUS_FAILED
                    record.finished_at = record.finished_at or _now_iso()
                    record.error = record.error or "Interrupted (dashboard restarted)"
            self._records = records
            return records

    def _save(self, records: List[JobRecord]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = [asdict(r) for r in records]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.path)
            self._records = list(records)

    # -- api ---------------------------------------------------------------

    def start(self, agent: str, params: Dict[str, Any]) -> JobRecord:
        record = JobRecord(id=uuid.uuid4().hex[:12], agent=agent, status=STATUS_RUNNING, params=params)
        with self._lock:
            records = self._load()
            records.insert(0, record)
            self._save(records[: self.limit])
        return record

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        summary: str = "",
        metrics: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            records = self._load()
            for record in records:
                if record.id != job_id:
                    continue
                record.status = status
                record.finished_at = _now_iso()
                try:
                    started = datetime.fromisoformat(record.started_at)
                    ended = datetime.fromisoformat(record.finished_at)
                    record.duration_seconds = round((ended - started).total_seconds(), 3)
                except ValueError:  # pragma: no cover - defensive
                    record.duration_seconds = None
                record.summary = summary
                record.metrics = dict(metrics or {})
                record.error = error
                self._save(records)
                return record
        return None

    def get(self, job_id: str) -> Optional[JobRecord]:
        for record in self._load():
            if record.id == job_id:
                return record
        return None

    def recent(self, limit: int = 25) -> List[JobRecord]:
        return self._load()[: max(1, int(limit))]

    def by_status(self, status: str, limit: int = 25) -> List[JobRecord]:
        wanted = str(status).strip().lower()
        return [r for r in self._load() if r.status == wanted][: max(1, int(limit))]

    def clear(self) -> None:
        with self._lock:
            self._save([])

    def counts(self) -> Dict[str, int]:
        records = self._load()
        counts = {STATUS_RUNNING: 0, STATUS_COMPLETED: 0, STATUS_FAILED: 0}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        counts["total"] = len(records)
        return counts

    def errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Recent failures, for the overview's error panel."""
        out: List[Dict[str, Any]] = []
        for record in self._load():
            if record.status != STATUS_FAILED:
                continue
            out.append(
                {
                    "id": record.id,
                    "agent": record.agent,
                    "at": record.finished_at or record.started_at,
                    "error": record.error or "Unknown error",
                }
            )
            if len(out) >= limit:
                break
        return out


class JobRunner:
    """Runs one agent call in a background thread and records it.

    The runner is intentionally unaware of the pipeline: ``invoke`` is supplied
    by the service and is expected to go through the Agent Manager. That keeps a
    single execution path and means the dashboard cannot accidentally run an
    agent some other way.
    """

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def submit(
        self,
        agent: str,
        params: Dict[str, Any],
        invoke: Callable[[], Dict[str, Any]],
    ) -> JobRecord:
        """Start ``invoke`` in the background and return its record immediately."""
        record = self.store.start(agent, params)

        def _target() -> None:
            try:
                outcome = invoke() or {}
                self.store.finish(
                    record.id,
                    status=STATUS_COMPLETED,
                    summary=str(outcome.get("summary") or ""),
                    metrics=outcome.get("metrics") or {},
                )
            except Exception as exc:  # noqa: BLE001 - a failed job is recorded, not raised
                # The traceback is logged for the developer; the API response
                # carries only the message, which the service has already
                # scrubbed of secret values.
                log.error("Job %s (%s) failed: %s", record.id, agent, exc)
                log.debug("%s", traceback.format_exc())
                self.store.finish(record.id, status=STATUS_FAILED, error=str(exc))

        thread = threading.Thread(target=_target, name=f"job-{record.id}", daemon=True)
        with self._lock:
            self._threads[record.id] = thread
        thread.start()
        return record

    def join(self, job_id: str, timeout: Optional[float] = None) -> None:
        """Block until a submitted job finishes. Used by tests."""
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)

    def running_count(self) -> int:
        return self.store.counts().get(STATUS_RUNNING, 0)


__all__ = [
    "JobRecord",
    "JobStore",
    "JobRunner",
    "STATUS_RUNNING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "RUNS_FILENAME",
    "DEFAULT_HISTORY_LIMIT",
]