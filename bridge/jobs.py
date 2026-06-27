"""Job manager for the direct-submit bridge.

Replaces the old polling/claiming model (intake.py, lifecycle.py, models.py).
Jobs arrive via HTTP POST, are queued in-memory, and executed one at a time
(oldest first). State is tracked in-memory and exposed via HTTP endpoints.

States (simplified from the old 16-state lifecycle):
  accepted → running → completed | failed | aborted | unknown_requires_operator_review

No eLabFTW polling. No metadata-encoded state machine. No claiming.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Job states ---

ACCEPTED = "accepted"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
ABORTED = "aborted"
UNKNOWN = "unknown_requires_operator_review"

TERMINAL_STATES = {COMPLETED, FAILED, ABORTED, UNKNOWN}


# --- Job data model ---


@dataclass
class Job:
    """A submitted execution job."""

    job_id: str
    title: str
    execution_mode: str  # "existing_protocol" or "generated_protocol"
    protocol_name: str = ""
    elabftw_experiment_id: int = 0
    spec_dict: dict[str, Any] = field(default_factory=dict)
    method_ref: dict[str, Any] = field(default_factory=dict)
    layout_ref: dict[str, Any] = field(default_factory=dict)
    analysis_ref: dict[str, Any] = field(default_factory=dict)
    expected_outputs: str = ""

    # Runtime state
    status: str = ACCEPTED
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    run_id: str = ""
    assay_prot_id: int = 0
    error: str = ""
    events: list[dict[str, str]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    spooled: bool = False

    # Abort
    abort_requested: bool = False

    def add_event(self, event: str, detail: str = "") -> None:
        self.events.append({"ts": now_iso(), "event": event, "detail": detail})

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "execution_mode": self.execution_mode,
            "protocol_name": self.protocol_name,
            "elabftw_experiment_id": self.elabftw_experiment_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "run_id": self.run_id,
            "assay_prot_id": self.assay_prot_id,
            "error": self.error,
            "events": list(self.events),
            "artifacts": list(self.artifacts),
            "spooled": self.spooled,
            "expected_outputs": self.expected_outputs,
        }


# --- Job manager ---


class JobManager:
    """Manages job lifecycle: receive, queue, execute, track state.

    Jobs are executed one at a time (oldest first) on a background thread.
    State is tracked in-memory and accessible via HTTP.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._current_job: Job | None = None
        self._executor: Any = None  # set by set_executor
        self._worker_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def set_executor(self, executor: Any) -> None:
        """Set the execution function: callable(job: Job) -> None."""
        self._executor = executor

    def submit_job(self, job_spec: dict[str, Any]) -> Job:
        """Accept a new job for execution.

        Args:
            job_spec: Job specification dict with title, execution_mode,
                protocol_name, etc.

        Returns:
            The created Job with job_id and status=accepted.
        """
        job = Job(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            title=job_spec.get("title", "Untitled"),
            execution_mode=job_spec.get("execution_mode", "existing_protocol"),
            protocol_name=job_spec.get("protocol_name", ""),
            elabftw_experiment_id=job_spec.get("elabftw_experiment_id", 0),
            spec_dict=job_spec.get("spec_dict", {}),
            method_ref=job_spec.get("method_ref", {}),
            layout_ref=job_spec.get("layout_ref", {}),
            analysis_ref=job_spec.get("analysis_ref", {}),
            expected_outputs=job_spec.get("expected_outputs", ""),
            created_at=now_iso(),
        )
        job.add_event("job_submitted")

        with self._lock:
            self._jobs[job.job_id] = job
            self._queue.append(job.job_id)

        logger.info("Job submitted: %s (%s)", job.job_id, job.title)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """List all jobs."""
        with self._lock:
            return list(self._jobs.values())

    def request_abort(self, job_id: str) -> bool:
        """Request abort for a job. Returns True if the job exists and is abortable."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in TERMINAL_STATES:
                return False
            job.abort_requested = True
            job.add_event("abort_requested")
            return True

    def start_worker(self) -> None:
        """Start the background worker thread that executes jobs."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Job worker thread started")

    def stop_worker(self) -> None:
        """Stop the background worker thread."""
        self._stop.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
        logger.info("Job worker thread stopped")

    def _worker_loop(self) -> None:
        """Main worker loop: pick jobs from queue and execute them."""
        while not self._stop.is_set():
            job_id: str | None = None
            with self._lock:
                if self._queue:
                    job_id = self._queue.popleft()
                    self._current_job = self._jobs.get(job_id)

            if job_id is None:
                time.sleep(0.5)
                continue

            job = self._jobs.get(job_id)
            if job is None:
                continue

            if self._executor is None:
                job.status = FAILED
                job.error = "No executor configured"
                job.add_event("execution_failed", "No executor configured")
                continue

            job.status = RUNNING
            job.started_at = now_iso()
            job.add_event("execution_started")

            try:
                self._executor(job)
                if job.status not in TERMINAL_STATES:
                    job.status = COMPLETED
                    job.add_event("execution_completed")
            except Exception as e:
                job.status = UNKNOWN
                job.error = f"Unexpected error: {e}"
                job.add_event("unexpected_error", str(e))
                logger.exception("Job %s failed unexpectedly", job_id)

            job.completed_at = now_iso()
            with self._lock:
                self._current_job = None

    @property
    def current_job(self) -> Job | None:
        """The currently executing job, if any."""
        with self._lock:
            return self._current_job
