"""FastAPI app for the Wallac Victor2 bridge — direct-submit HTTP API.

Replaces the old polling daemon (main.py). The bridge no longer polls
eLabFTW for jobs. Instead, it accepts job submissions via HTTP POST and
executes them on a background worker thread.

Endpoints:
  GET  /health           — backwards-compatible health summary
  GET  /health/live      — process liveness check
  GET  /health/ready     — worker and dependency readiness check
  POST /jobs             — submit a job for execution
  GET  /jobs             — list all jobs
  GET  /jobs/{job_id}    — get job status
  POST /jobs/{job_id}/abort — abort a running job

Authentication: bearer token via WALLAC_BRIDGE_TOKEN env var.
If unset, auth is disabled (dev mode only).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from .config import BridgeConfig
from .elabftw import ElabftwClient
from .executor import BridgeExecutor
from .jobs import DuplicateJobError, Job, JobManager
from .security_headers import install_security_headers
from .vm_agent_client import VmAgentClient

# --- Pydantic models ---


# 8 rows (A..H) by 12 columns (1..12). Used to expand ``{all: true}`` and
# ``{rows: [...]}`` server-side so the vm-agent only ever sees the
# canonical ``{wells: [...]}`` shape. Duplicating the constants here keeps
# the public Pydantic model independent of vm-agent internals.
_VALID_ROWS = "ABCDEFGH"
_WELL_PATTERN_RE = r"^[A-H](?:[1-9]|1[0-2])$"


class WellsSpec(BaseModel):
    """Public ``wells_spec`` contract.

    Accepts ONE of the following keys:

    * ``all: true`` — measure every well on the 96-well plate.
    * ``rows: ["A", "B", ...]`` — measure every well in the named rows.
    * ``wells: ["A1", "A2", ...]`` — measure only the named wells.

    Anything else (multiple keys at once, a non-list ``wells`` value, an
    unsupported key, a row outside A..H, a well outside A1..H12) is
    rejected with HTTP 400 *before* the job is queued, so the vm-agent
    never sees garbage shapes.
    """

    all: bool = False
    rows: list[str] | None = None
    wells: list[str] | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_shape(self) -> WellsSpec:
        import re as _re

        chosen = [
            name
            for name, value in (
                ("all", self.all),
                ("rows", self.rows),
                ("wells", self.wells),
            )
            if value
        ]
        if len(chosen) == 0:
            # Empty spec is valid: caller wants the factory plate map.
            self.all = False
            self.rows = None
            self.wells = None
            return self
        if len(chosen) > 1:
            raise ValueError(f"wells_spec accepts exactly one of all/rows/wells; got {chosen}")
        if self.rows is not None:
            cleaned: list[str] = []
            for row in self.rows:
                token = str(row).strip().upper()
                if len(token) != 1 or token not in _VALID_ROWS:
                    raise ValueError(
                        f"wells_spec.rows must be a list of single letters A..H; got {row!r}"
                    )
                cleaned.append(token)
            self.rows = cleaned
        if self.wells is not None:
            cleaned_wells: list[str] = []
            for well in self.wells:
                token = str(well).strip().upper()
                if not _re.match(_WELL_PATTERN_RE, token):
                    raise ValueError(f"wells_spec.wells must look like A1..H12; got {well!r}")
                cleaned_wells.append(token)
            self.wells = cleaned_wells
        return self

    def expanded(self) -> list[str] | None:
        """Return the canonical ``wells: [...]`` form, or ``None`` if empty.

        ``None`` means "use the factory plate map"; an empty list means
        "explicit empty plate" (which the vm-agent rejects, but only the
        executor needs to handle that). The bridge expands ``all`` and
        ``rows`` here so the vm-agent only ever sees the canonical shape.
        """
        if self.all:
            return [f"{r}{c}" for r in _VALID_ROWS for c in range(1, 13)]
        if self.rows:
            return [f"{r}{c}" for r in self.rows for c in range(1, 13)]
        if self.wells:
            return list(self.wells)
        return None

    def to_slim_dict(self) -> dict[str, Any]:
        """Return only the populated fields, for round-trip serialization.

        Pydantic's default ``model_dump`` emits all three keys with
        ``None`` siblings, which leaks internal model state to clients
        and breaks the round-trip expectation ``body["wells_spec"] ==
        submitted_spec``. Use this helper to keep the wire shape stable.
        """
        if self.all:
            return {"all": True}
        if self.rows is not None:
            return {"rows": list(self.rows)}
        if self.wells is not None:
            return {"wells": list(self.wells)}
        return {}


class JobSubmitRequest(BaseModel):
    title: str = Field(..., description="Human-readable job title")
    execution_mode: str = Field(
        "existing_protocol", description="existing_protocol or generated_protocol"
    )
    protocol_name: str = Field("", description="Wallac protocol name (existing_protocol mode)")
    protocol_id: int = Field(
        0,
        description="Wallac protocol ID (existing_protocol mode, takes precedence over protocol_name)",
    )
    elabftw_experiment_id: int = Field(0, description="eLabFTW experiment ID for result write-back")
    wells_spec: WellsSpec = Field(
        default_factory=WellsSpec,
        description=(
            "Optional plate-map override. Accepts {all: true}, "
            "{rows: [A,B]}, or {wells: [A1,A2]}. "
            "For existing_protocol the bridge clones the resolved protocol "
            "into a per-run id, applies the override on the clone, runs on "
            "the clone, and deletes the clone when the run ends — the "
            "factory preset is never written to. "
            "For generated_protocol the plate map is derived from the signed "
            "Layout spec; this field is accepted for forward compatibility "
            "but currently ignored."
        ),
    )
    expected_outputs: str = Field("", description="Expected measurement outputs")
    spec_dict: dict[str, Any] = Field(
        default_factory=dict, description="Parsed job spec (generated_protocol mode)"
    )
    method_ref: dict[str, Any] = Field(default_factory=dict, description="Signed Method reference")
    layout_ref: dict[str, Any] = Field(default_factory=dict, description="Signed Layout reference")
    analysis_ref: dict[str, Any] = Field(
        default_factory=dict, description="Signed Analysis reference"
    )


class JobResponse(BaseModel):
    job_id: str
    title: str
    execution_mode: str
    protocol_name: str
    protocol_id: int
    elabftw_experiment_id: int
    wells_spec: dict[str, Any] = {}
    status: str
    created_at: str
    started_at: str
    completed_at: str
    run_id: str
    assay_prot_id: int
    error: str
    events: list[dict[str, str]]
    artifacts: list[dict[str, Any]]
    spooled: bool
    expected_outputs: str
    live_wells: list[dict[str, Any]] = []


class AbortResponse(BaseModel):
    job_id: str
    abort_requested: bool


class RetryWritebackResponse(BaseModel):
    job_id: str
    retried: bool
    status: str
    reason: str = ""
    elabftw_experiment_id: int = 0


def _job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        title=job.title,
        execution_mode=job.execution_mode,
        protocol_name=job.protocol_name,
        protocol_id=job.protocol_id,
        elabftw_experiment_id=job.elabftw_experiment_id,
        wells_spec=job.wells_spec,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        run_id=job.run_id,
        assay_prot_id=job.assay_prot_id,
        error=job.error,
        events=list(job.events),
        artifacts=list(job.artifacts),
        spooled=job.spooled,
        expected_outputs=job.expected_outputs,
        live_wells=list(job.live_wells),
    )


def _check_auth(token: str, authorization: str | None) -> None:
    """Check bearer token. No-op if token is empty (dev mode).

    Uses ``hmac.compare_digest`` for constant-time comparison. The check is
    deliberately tolerant of length differences — ``compare_digest`` returns
    False (without raising) when lengths differ, so we don't need a guard.
    """
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    presented = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def _bridge_token(config: BridgeConfig | None) -> str:
    """Read the bridge token and enforce strict-auth startup policy."""
    token = os.environ.get("WALLAC_BRIDGE_TOKEN", "")
    if config is not None and config.require_auth and not token:
        raise RuntimeError(
            "WALLAC_REQUIRE_AUTH is set but WALLAC_BRIDGE_TOKEN is empty; "
            "refusing to start the bridge with auth disabled. "
            "Either unset WALLAC_REQUIRE_AUTH or set WALLAC_BRIDGE_TOKEN."
        )
    return token


def _wire_executor(config: BridgeConfig, job_manager: JobManager) -> BridgeExecutor:
    """Connect the job manager to configured adapters and return the executor."""
    vm_agent = VmAgentClient(base_url=config.vm_agent_url, token=config.vm_agent_token)
    elabftw = ElabftwClient(
        base_url=config.elabftw_url,
        api_key=config.elabftw_api_key,
        verify_tls=config.elabftw_verify_tls,
        ca_bundle=config.elabftw_ca_bundle,
    )
    executor = BridgeExecutor(vm_agent=vm_agent, elabftw=elabftw, dry_run=config.dry_run)
    job_manager.set_executor(executor)
    job_manager.start_worker()
    return executor


def _configure_cors(app: FastAPI, config: BridgeConfig | None) -> None:
    """Apply the explicit browser-origin allowlist when configured."""
    cors_origins = list(config.cors_origins) if config is not None else []
    if not cors_origins:
        return
    if "*" in cors_origins:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "WALLAC_CORS_ORIGINS includes '*'; the bridge API will "
            "respond to any origin. Use an explicit allowlist."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def _health_ready_response(
    manager: JobManager, executor: BridgeExecutor | None
) -> tuple[dict[str, Any], bool]:
    """Build the /health/ready response dict and ready flag."""
    issues: list[str] = []
    worker_running = manager.worker_running
    if not worker_running:
        issues.append("worker_not_running")
    if executor is None:
        issues.append("dependencies_not_configured")
    else:
        try:
            executor.elabftw.check_connection(timeout=1.5)

        except Exception:
            issues.append("elabftw_unavailable")
        try:
            executor.vm_agent.get_health(timeout=1.5)

        except Exception:
            issues.append("vm_agent_unavailable")
    ready = not issues
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "worker_running": worker_running,
        "issues": issues,
    }, ready


def _register_health_routes(
    app: FastAPI,
    manager: JobManager,
    executor: BridgeExecutor | None,
) -> None:
    """Register liveness/readiness endpoints on the app."""

    @app.get("/health")
    def health() -> dict[str, Any]:
        current = manager.current_job
        return {
            "status": "ok",
            "worker_running": manager.worker_running,
            "current_job": current.job_id if current else "",
        }

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(response: Response) -> dict[str, Any]:
        body, ready = _health_ready_response(manager, executor)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return body


def _resolve_retry_writeback_target(
    manager: JobManager,
    job_id: str,
    executor: BridgeExecutor | None,
) -> tuple[Any, HTTPException | None]:
    """Validate a retry-writeback request and return the target Job.

    Returns ``(job, None)`` when the request is valid, or
    ``(None, HTTPException)`` when the request must be rejected with
    the indicated HTTP status. Splitting this from the handler keeps
    ``_register_routes`` under the complexity ceiling.
    """
    job = manager.get_job(job_id)
    if job is None:
        return None, HTTPException(status_code=404, detail=f"Job {job_id} not found")
    from bridge.jobs import TERMINAL_STATES

    if job.status not in TERMINAL_STATES:
        return None, HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} is not terminal (status={job.status!r}); "
                "writeback retry is only meaningful after the hardware run finishes"
            ),
        )
    if not job.live_wells:
        return None, HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} has no live_wells data; the original run never produced "
                "usable readings and writeback cannot be reconstructed"
            ),
        )
    if executor is None:
        return None, HTTPException(
            status_code=503,
            detail="executor not configured",
        )
    return job, None


def _build_retry_writeback_response(job_id: str, job: Any) -> RetryWritebackResponse:
    """Build the response payload after ``executor.retry_writeback`` ran.

    The executor may set the job status back to ``completed`` on success
    or leave it in ``unknown_requires_operator_review`` on failure — the
    response reflects the post-call state.
    """
    retry_event = next(
        (
            evt
            for evt in reversed(job.events)
            if evt["event"] in {"writeback_completed", "writeback_retry_rejected"}
        ),
        None,
    )
    retried = retry_event is not None and retry_event["event"] == "writeback_completed"
    return RetryWritebackResponse(
        job_id=job_id,
        retried=retried,
        status=job.status,
        reason=retry_event["detail"] if retry_event else "",
        elabftw_experiment_id=job.elabftw_experiment_id,
    )


def _register_routes(
    app: FastAPI,
    manager: JobManager,
    token: str,
    executor: BridgeExecutor | None,
) -> None:
    """Register the bridge HTTP contract against one job manager."""

    _register_health_routes(app, manager, executor)

    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    def submit_job(
        req: JobSubmitRequest, authorization: str | None = Header(default=None)
    ) -> JobResponse:
        _check_auth(token, authorization)
        try:
            # Reason: WellsSpec is a typed Pydantic model so default
            # ``model_dump`` includes all three sibling keys with Nones.
            # Normalize to the slim form so the stored Job.wells_spec
            # matches what the caller submitted — keeps the public
            # contract stable and prevents accidental garbage reaching
            # the executor / vm-agent.
            payload = req.model_dump()
            payload["wells_spec"] = req.wells_spec.to_slim_dict()
            job = manager.submit_job(payload)
        except DuplicateJobError as e:
            existing = manager.get_job(e.existing_job_id)
            detail: dict[str, Any] = {
                "message": "Duplicate job",
                "existing_job_id": e.existing_job_id,
            }
            if existing is not None:
                detail.update(
                    message="A job with the same spec is already active",
                    existing_status=existing.status,
                )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from e
        return _job_to_response(job)

    @app.get("/jobs", response_model=list[JobResponse])
    def list_jobs(authorization: str | None = Header(default=None)) -> list[JobResponse]:
        _check_auth(token, authorization)
        return [_job_to_response(job) for job in manager.list_jobs()]

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, authorization: str | None = Header(default=None)) -> JobResponse:
        _check_auth(token, authorization)
        job = manager.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return _job_to_response(job)

    @app.post("/jobs/{job_id}/abort", response_model=AbortResponse)
    def abort_job(job_id: str, authorization: str | None = Header(default=None)) -> AbortResponse:
        _check_auth(token, authorization)
        if not manager.request_abort(job_id):
            raise HTTPException(status_code=409, detail=f"Job {job_id} not found or not abortable")
        return AbortResponse(job_id=job_id, abort_requested=True)

    @app.post(
        "/jobs/{job_id}/retry-writeback",
        response_model=RetryWritebackResponse,
    )
    def retry_writeback(
        job_id: str, authorization: str | None = Header(default=None)
    ) -> RetryWritebackResponse:
        """Re-run eLabFTW writeback for a job whose hardware run already
        completed but whose writeback failed (e.g. transient TLS blip,
        eLabFTW restart). MUST NOT restart the hardware run.

        Slice 5 of
        ``docs/plans/wallac-existing-protocol-writeback-repair.md``.

        Returns 404 if the job is unknown, 409 if the job is in a state
        where retry is meaningless (not terminal, or no live_wells
        data), and 200 otherwise. The handler does not block on the
        retry itself; ``executor.retry_writeback`` is fast (HTTP PATCH
        to eLabFTW) and any failure is recorded on the job's events.
        """
        _check_auth(token, authorization)
        job, error_response = _resolve_retry_writeback_target(manager, job_id, executor)
        if error_response is not None:
            raise error_response
        # ``_resolve_retry_writeback_target`` guarantees ``executor`` is
        # non-None when ``error_response`` is None — see the 503 branch.
        executor.retry_writeback(job)  # type: ignore[union-attr]
        return _build_retry_writeback_response(job_id, job)


# --- App factory ---


def create_bridge_app(
    config: BridgeConfig | None = None,
    job_manager: JobManager | None = None,
    *,
    executor: Any | None = None,
) -> FastAPI:
    """Create the FastAPI bridge app.

    Args:
        config: Bridge config (for production). If None, reads from env.
        job_manager: Pre-configured JobManager (for testing). If None, creates one.
        executor: Optional executor to wire when ``config`` is None (the
            test-only path). Lets unit tests drive the retry-writeback
            endpoint without booting the production wiring.
    """
    if config is None and job_manager is None:
        config = BridgeConfig.from_env()
    manager = job_manager or JobManager()
    token = _bridge_token(config)
    if config is not None:
        executor = _wire_executor(config, manager)
    # else: ``executor`` is whatever the caller supplied (or None).

    app = FastAPI(
        title="Wallac Victor2 Bridge",
        description="Direct-submit HTTP API for instrument execution",
        version="2.0.0",
    )

    install_security_headers(app)
    _configure_cors(app, config)
    _register_routes(app, manager, token, executor)
    return app
