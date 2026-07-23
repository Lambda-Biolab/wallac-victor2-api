"""FastAPI app for the Wallac Victor2 bridge — direct-submit HTTP API.

Replaces the old polling daemon (main.py). The bridge no longer polls
eLabFTW for jobs. Instead, it accepts job submissions via HTTP POST and
executes them on a background worker thread.

Endpoints:
  GET  /health           — bridge health check
  POST /jobs             — submit a job for execution
  GET  /jobs             — list all jobs
  GET  /jobs/{job_id}    — get job status
  POST /jobs/{job_id}/abort — abort a running job

Authentication: bearer token via WALLAC_BRIDGE_TOKEN env var.
If unset, auth is disabled (dev mode only).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import BridgeConfig
from .elabftw import ElabftwClient
from .executor import BridgeExecutor
from .jobs import DuplicateJobError, Job, JobManager
from .vm_agent_client import VmAgentClient

# --- Pydantic models ---


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
    wells_spec: dict[str, Any] = Field(
        default_factory=dict,
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
    """Check bearer token. No-op if token is empty (dev mode)."""
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    if authorization.removeprefix("Bearer ") != token:
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


def _wire_executor(config: BridgeConfig, job_manager: JobManager) -> None:
    """Connect the job manager to the configured external adapters."""
    vm_agent = VmAgentClient(base_url=config.vm_agent_url, token=config.vm_agent_token)
    elabftw = ElabftwClient(
        base_url=config.elabftw_url,
        api_key=config.elabftw_api_key,
        verify_tls=config.elabftw_verify_tls,
    )
    job_manager.set_executor(
        BridgeExecutor(vm_agent=vm_agent, elabftw=elabftw, dry_run=config.dry_run)
    )
    job_manager.start_worker()


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


def _register_routes(app: FastAPI, manager: JobManager, token: str) -> None:
    """Register the bridge HTTP contract against one job manager."""

    @app.get("/health")
    def health() -> dict[str, Any]:
        current = manager.current_job
        return {
            "status": "ok",
            "worker_running": (
                manager._worker_thread is not None and manager._worker_thread.is_alive()
            ),
            "current_job": current.job_id if current else "",
        }

    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    def submit_job(
        req: JobSubmitRequest, authorization: str | None = Header(default=None)
    ) -> JobResponse:
        _check_auth(token, authorization)
        try:
            job = manager.submit_job(req.model_dump())
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


# --- App factory ---


def create_bridge_app(
    config: BridgeConfig | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    """Create the FastAPI bridge app.

    Args:
        config: Bridge config (for production). If None, reads from env.
        job_manager: Pre-configured JobManager (for testing). If None, creates one.
    """
    if config is None and job_manager is None:
        config = BridgeConfig.from_env()
    manager = job_manager or JobManager()
    token = _bridge_token(config)
    if config is not None:
        _wire_executor(config, manager)

    app = FastAPI(
        title="Wallac Victor2 Bridge",
        description="Direct-submit HTTP API for instrument execution",
        version="2.0.0",
    )

    _configure_cors(app, config)
    _register_routes(app, manager, token)
    return app
