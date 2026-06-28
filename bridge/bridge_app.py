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
from .jobs import Job, JobManager

# --- Pydantic models ---


class JobSubmitRequest(BaseModel):
    title: str = Field(..., description="Human-readable job title")
    execution_mode: str = Field(
        "existing_protocol", description="existing_protocol or generated_protocol"
    )
    protocol_name: str = Field("", description="Wallac protocol name (existing_protocol mode)")
    elabftw_experiment_id: int = Field(0, description="eLabFTW experiment ID for result write-back")
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
    elabftw_experiment_id: int
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


class AbortResponse(BaseModel):
    job_id: str
    abort_requested: bool


def _job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        title=job.title,
        execution_mode=job.execution_mode,
        protocol_name=job.protocol_name,
        elabftw_experiment_id=job.elabftw_experiment_id,
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

    if job_manager is None:
        job_manager = JobManager()

    bridge_token = os.environ.get("WALLAC_BRIDGE_TOKEN", "")

    app = FastAPI(
        title="Wallac Victor2 Bridge",
        description="Direct-submit HTTP API for instrument execution",
        version="2.0.0",
    )

    # Allow the Run Builder (different port) to call the bridge API
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Store references for closures
    mgr = job_manager
    token = bridge_token

    @app.get("/health")
    def health() -> dict[str, Any]:
        current = mgr.current_job
        return {
            "status": "ok",
            "worker_running": mgr._worker_thread is not None and mgr._worker_thread.is_alive(),
            "current_job": current.job_id if current else "",
        }

    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    def submit_job(
        req: JobSubmitRequest, authorization: str | None = Header(default=None)
    ) -> JobResponse:
        _check_auth(token, authorization)
        job = mgr.submit_job(req.model_dump())
        return _job_to_response(job)

    @app.get("/jobs", response_model=list[JobResponse])
    def list_jobs(authorization: str | None = Header(default=None)) -> list[JobResponse]:
        _check_auth(token, authorization)
        return [_job_to_response(j) for j in mgr.list_jobs()]

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, authorization: str | None = Header(default=None)) -> JobResponse:
        _check_auth(token, authorization)
        job = mgr.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return _job_to_response(job)

    @app.post("/jobs/{job_id}/abort", response_model=AbortResponse)
    def abort_job(job_id: str, authorization: str | None = Header(default=None)) -> AbortResponse:
        _check_auth(token, authorization)
        ok = mgr.request_abort(job_id)
        if not ok:
            raise HTTPException(status_code=409, detail=f"Job {job_id} not found or not abortable")
        return AbortResponse(job_id=job_id, abort_requested=True)

    return app
