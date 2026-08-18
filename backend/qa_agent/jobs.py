from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from .config import Settings, settings
from .db import Database, database
from .utils import new_id, utc_now


class JobManager:
    """Bounded in-process job runner.

    This is intentionally single-node and lightweight: no Redis/Celery process is
    required for local deployment. Job state is persisted in SQLite. Long AI/HTTP
    work runs in a small thread pool and never blocks FastAPI request threads.
    """

    def __init__(self, db: Database = database, config: Settings = settings):
        self.db = db
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=config.job_workers, thread_name_prefix="qa-job")
        self._locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        # In-process workers intentionally keep the local deployment lightweight.
        # A restart cannot resume Python call stacks, so stale jobs are made
        # explicitly retryable instead of appearing to run forever.
        self.db.fail_incomplete_jobs()

    def submit(self, project_id: str | None, kind: str, fn: Callable[[Callable[[int, str], None]], Any], serial_key: str | None = None) -> Dict[str, Any]:
        job = {
            "id": new_id("job"), "project_id": project_id, "kind": kind, "status": "queued", "progress": 0,
            "message": "Queued", "result": None, "error": "", "created_at": utc_now(), "updated_at": utc_now(),
        }
        self.db.save_job(job)
        self.executor.submit(self._run, job, fn, serial_key)
        return job

    def _run(self, job: Dict[str, Any], fn: Callable[[Callable[[int, str], None]], Any], serial_key: str | None = None) -> None:
        lock = None
        if serial_key:
            with self._lock:
                lock = self._locks.setdefault(serial_key, threading.Lock())
        if lock:
            lock.acquire()
        self._update(job, status="running", progress=1, message="Starting…")
        def progress(value: int, message: str):
            self._update(job, progress=max(0, min(int(value), 99)), message=message)
        try:
            result = fn(progress)
            self._update(job, status="completed", progress=100, message="Completed", result=result)
        except Exception as exc:
            self._update(job, status="failed", progress=100, message="Failed", error=str(exc))
        finally:
            if lock:
                lock.release()

    def _update(self, job: Dict[str, Any], **changes: Any) -> None:
        job.update(changes)
        job["updated_at"] = utc_now()
        self.db.save_job(job)


job_manager = JobManager()
