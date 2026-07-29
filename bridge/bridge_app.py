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
import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from .config import BridgeConfig
from .durable.endpoints import register_writeback_routes
from .durable.manager import JobManager as DurableJobManager
from .elabftw import ElabftwClient
from .executor import BridgeExecutor
from .jobs import DuplicateJobError, Job, JobManager
from .security_headers import install_security_headers
from .vm_agent_client import VmAgentClient

logger = logging.getLogger(__name__)

# --- Pydantic models ---


# 8 rows (A..H) by 12 columns (1..12). Used to expand ``{all: true}`` and
# ``{rows: [...]}`` server-side so the vm-agent only ever sees the
# canonical ``{wells: [...]}`` shape. Duplicating the constants here keeps
# the public Pydantic model independent of vm-agent internals.
_VALID_ROWS = "ABCDEFGH"
# Strict 1..12 well address with no zero-padding. The vm-agent parses
# zero-padded names (e.g. ``A01``) by extracting the digits and
# range-checking the integer, but the bridge's public contract uses
# the canonical ``A1..H12`` form. Review NIT: callers sending
# ``A01`` will see HTTP 422 at the boundary — round-trip to the
# canonical form before resubmitting.
_WELL_PATTERN_RE = r"^[A-H](?:[1-9]|1[0-2])$"


def _clean_rows(rows: list[str]) -> list[str]:
    """Normalize and validate ``wells_spec.rows`` entries (A..H).

    Extracted from ``WellsSpec._validate_shape`` to keep the model's
    complexity under the project ceiling. Raises ``ValueError`` on
    any row outside A..H.
    """
    cleaned: list[str] = []
    for row in rows:
        token = str(row).strip().upper()
        if len(token) != 1 or token not in _VALID_ROWS:
            raise ValueError(f"wells_spec.rows must be a list of single letters A..H; got {row!r}")
        cleaned.append(token)
    return cleaned


def _clean_wells(wells: list[str]) -> list[str]:
    """Normalize and validate ``wells_spec.wells`` entries (A1..H12).

    Extracted from ``WellsSpec._validate_shape`` to keep the model's
    complexity under the project ceiling. Raises ``ValueError`` on
    any well outside the canonical address space.
    """
    import re as _re

    cleaned: list[str] = []
    for well in wells:
        token = str(well).strip().upper()
        if not _re.match(_WELL_PATTERN_RE, token):
            raise ValueError(f"wells_spec.wells must look like A1..H12; got {well!r}")
        cleaned.append(token)
    return cleaned


class WellsSpec(BaseModel):
    """Public ``wells_spec`` contract.

    Accepts ONE of the following keys (presence-based, not truthiness):

    * ``all: true`` — measure every well on the 96-well plate.
      ``all: false`` is rejected so a malformed caller cannot silently
      fall back to the factory plate map.
    * ``rows: ["A", "B", ...]`` — measure every well in the named rows.
      The list MUST be non-empty; an empty list is rejected at the
      boundary.
    * ``wells: ["A1", "A2", ...]`` — measure only the named wells.
      The list MUST be non-empty; an empty list is rejected at the
      boundary.

    Anything else (multiple keys at once, an empty list, an
    unsupported key, a row outside A..H, a well outside A1..H12) is
    rejected with HTTP 422 *before* the job is queued, so the vm-agent
    never sees garbage shapes.

    To run the protocol's factory 96-well plate map unchanged, omit
    the field entirely or pass ``{}``.
    """

    model_config = {"extra": "forbid"}

    all: bool | None = None
    rows: list[str] | None = None
    wells: list[str] | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> WellsSpec:
        # Presence-based, not truthiness: ``{"all": false}`` and
        # ``{"rows": []}`` are explicitly rejected so a malformed
        # caller cannot accidentally fall back to the factory plate
        # map when they meant to constrain the run.
        chosen: list[str] = []
        if "all" in self.model_fields_set:
            chosen.append("all")
            if self.all is not True:
                raise ValueError("wells_spec.all must be exactly true when present")
        if "rows" in self.model_fields_set:
            chosen.append("rows")
            if not self.rows:
                raise ValueError("wells_spec.rows must be a non-empty list when present")
        if "wells" in self.model_fields_set:
            chosen.append("wells")
            if not self.wells:
                raise ValueError("wells_spec.wells must be a non-empty list when present")

        if len(chosen) == 0:
            # Empty/absent spec — caller wants the factory plate map.
            return self
        if len(chosen) > 1:
            raise ValueError(
                f"wells_spec accepts exactly one of all/rows/wells; got {sorted(chosen)}"
            )
        if self.rows is not None:
            self.rows = _clean_rows(self.rows)
        if self.wells is not None:
            self.wells = _clean_wells(self.wells)
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


def _wire_executor(
    config: BridgeConfig,
    job_manager: JobManager,
    *,
    durable_manager: Any | None = None,
    durable_ledger: Any | None = None,
) -> tuple[BridgeExecutor, ElabftwClient]:
    """Connect the job manager to configured adapters and return them.

    When ``durable_manager`` and ``durable_ledger`` are supplied, the
    executor takes the durable writeback path (issue #44): it spools
    raw/analyzed/body artifacts to ``${STATE_DIR}/spool/<job_id>/``
    and enqueues the four canonical writeback steps. The actual
    eLabFTW operations are then performed by the ``WritebackWorker``
    driven by the ``WritebackDispatcher`` (constructed in
    ``create_bridge_app`` after this returns).

    The returned :class:`ElabftwClient` is the same instance the
    executor uses so the dispatcher and the executor share a single
    HTTP client (and any per-instance state — connection pool, retry
    config).
    """
    vm_agent = VmAgentClient(base_url=config.vm_agent_url, token=config.vm_agent_token)
    elabftw = ElabftwClient(
        base_url=config.elabftw_url,
        api_key=config.elabftw_api_key,
        verify_tls=config.elabftw_verify_tls,
        ca_bundle=config.elabftw_ca_bundle,
    )
    executor = BridgeExecutor(
        vm_agent=vm_agent,
        elabftw=elabftw,
        dry_run=config.dry_run,
        durable_manager=durable_manager,
        durable_ledger=durable_ledger,
    )
    job_manager.set_executor(executor)
    job_manager.start_worker()
    return executor, elabftw


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

    Retry eligibility (review-blocker 3): only ``completed`` and
    ``unknown_requires_operator_review`` jobs. ``failed`` and
    ``aborted`` jobs are refused because:

    * An aborted run can have partial ``live_wells`` accumulated
      before the abort landed — retrying those would publish a
      partial measurement set marked as completed.
    * A failed run typically has no recoverable data.
    """
    job = manager.get_job(job_id)
    if job is None:
        return None, HTTPException(status_code=404, detail=f"Job {job_id} not found")
    from bridge.jobs import COMPLETED, UNKNOWN

    if job.status not in (COMPLETED, UNKNOWN):
        return None, HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} is not retry-eligible (status={job.status!r}); "
                "only completed or unknown_requires_operator_review jobs can be retried"
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


def _build_retry_writeback_response(
    job_id: str, job: Any, *, success: bool
) -> RetryWritebackResponse:
    """Build the response payload after ``executor.retry_writeback`` ran.

    Review concern round 3: the ``success`` flag is the authoritative
    outcome — the caller passes the return value of
    ``executor.retry_writeback``. Searching ``job.events`` for the
    latest retry event is unreliable under concurrent retry requests
    because two requests can interleave events and confuse the
    search. The flag bypasses that race entirely.

    The function still picks up the most recent retry event for the
    human-readable ``reason`` field — that is informational and does
    not influence the ``retried`` boolean.
    """
    retry_event = next(
        (
            evt
            for evt in reversed(job.events)
            if evt["event"]
            in ("writeback_retry_completed", "writeback_retry_failed", "writeback_retry_rejected")
        ),
        None,
    )
    return RetryWritebackResponse(
        job_id=job_id,
        retried=success,
        status=job.status,
        reason=retry_event["detail"] if retry_event else "",
        elabftw_experiment_id=job.elabftw_experiment_id,
    )


def _submit_job_to_both_ledgers(
    in_memory: JobManager,
    durable: Any | None,
    payload: dict[str, Any],
) -> Any:
    """Submit a job to both the in-memory and durable ledgers.

    The durable insert runs first (so the in-memory worker cannot
    dequeue + start physical execution before the durable row is
    committed) and the in-memory ``submit_job`` follows. If the
    in-memory submission raises ``DuplicateJobError``, the durable
    row is deleted (compensating action — re-review round 4
    blocker #1) so the recovery bundle does not show an orphan.
    """
    pre_job_id = f"job-{uuid.uuid4().hex[:16]}"
    if durable is not None:
        durable.submit_job(
            job_id=pre_job_id,
            title=payload.get("title", "Untitled"),
            execution_mode=payload.get("execution_mode", "existing_protocol"),
            protocol_name=payload.get("protocol_name", ""),
            protocol_id=payload.get("protocol_id", 0),
            elabftw_experiment_id=payload.get("elabftw_experiment_id", 0),
            wells_spec=payload.get("wells_spec", {}),
        )
    try:
        return in_memory.submit_job(payload, job_id=pre_job_id)
    except DuplicateJobError as exc:
        if durable is not None:
            try:
                durable.delete_orphan(pre_job_id)
            except Exception:
                logger.exception("failed to delete orphan durable row %s", pre_job_id)
        existing = in_memory.get_job(exc.existing_job_id)
        detail: dict[str, Any] = {
            "message": "Duplicate job",
            "existing_job_id": exc.existing_job_id,
        }
        if existing is not None:
            detail.update(
                message="A job with the same spec is already active",
                existing_status=existing.status,
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc


def _register_routes(
    app: FastAPI,
    manager: JobManager,
    token: str,
    executor: BridgeExecutor | None,
    *,
    durable_manager: Any | None = None,
) -> None:
    """Register the bridge HTTP contract against one job manager."""

    _register_health_routes(app, manager, executor)

    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    def submit_job(
        req: JobSubmitRequest, authorization: str | None = Header(default=None)
    ) -> JobResponse:
        _check_auth(token, authorization)
        # Reason: WellsSpec is a typed Pydantic model so default
        # ``model_dump`` includes all three sibling keys with Nones.
        # Normalize to the slim form so the stored Job.wells_spec
        # matches what the caller submitted — keeps the public
        # contract stable and prevents accidental garbage reaching
        # the executor / vm-agent.
        payload = req.model_dump()
        payload["wells_spec"] = req.wells_spec.to_slim_dict()
        return _job_to_response(_submit_job_to_both_ledgers(manager, durable_manager, payload))

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

        Returns 200 when the retry succeeded, 503 when the retry
        attempt itself failed (eLabFTW still unreachable, see the
        underlying ``elabftw_writeback_failed`` event), 404 if the
        job is unknown, 409 if the job is in a state where retry is
        meaningless (not completed/unknown-review, or no live_wells
        data), and 503 if the executor is not wired (test/dev path).

        The handler does not block on the retry itself;
        ``executor.retry_writeback`` is fast (HTTP PATCH to eLabFTW).
        The success/failure outcome comes from its return value, not
        from searching ``job.events`` — concurrent retry requests
        can interleave events on the shared event list.
        """
        _check_auth(token, authorization)
        job, error_response = _resolve_retry_writeback_target(manager, job_id, executor)
        if error_response is not None:
            raise error_response
        # ``_resolve_retry_writeback_target`` guarantees ``executor`` is
        # non-None when ``error_response`` is None — see the 503 branch.
        # Review concern round 3: use the return value as the
        # authoritative outcome. Two concurrent retry requests can
        # interleave events on the shared job.events list, so we
        # MUST NOT re-derive success by searching the events.
        success = executor.retry_writeback(job)  # type: ignore[union-attr]
        if not success:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Retry writeback for job {job_id} did not succeed; "
                    "see job events for the underlying error"
                ),
            )
        return _build_retry_writeback_response(job_id, job, success=True)


def _start_durable_worker(
    in_memory_manager: JobManager,
    durable_manager: Any,
    elabftw: Any,
) -> Any:
    """Build + start the durable ``WritebackWorker`` and return it.

    Mirrors the dispatcher's eLabFTW operations to the in-memory
    ``Job`` so legacy event observers (``writeback_completed``,
    ``execution_completed``) fire the same way they do on the
    synchronous path. Returns the running ``WritebackWorker``;
    callers are responsible for stopping it on app shutdown.
    """
    from bridge.durable.dispatcher import WritebackDispatcher
    from bridge.durable.manager import now_iso as _now
    from bridge.durable.worker import WritebackWorker

    def _on_step_complete(job_id: str, action: str, exp_id: str) -> None:
        mem_job = in_memory_manager.get_job(job_id)
        if mem_job is None:
            return
        if action == "create_experiment" and mem_job.elabftw_experiment_id == 0:
            mem_job.elabftw_experiment_id = int(exp_id)
            mem_job.add_event("experiment_created", exp_id)
        elif action == "upload_raw":
            mem_job.add_event("raw_results_uploaded", "")
        elif action == "upload_analyzed":
            mem_job.add_event("analyzed_results_uploaded", "")

    def _on_all_steps_done(job_id: str) -> None:
        mem_job = in_memory_manager.get_job(job_id)
        if mem_job is not None and mem_job.status not in ("failed", "aborted"):
            mem_job.status = "completed"
            mem_job.add_event("writeback_completed", "durable")
            mem_job.add_event("execution_completed", "")
        durable_manager.mark_status(job_id, "completed", completed_at=_now())
        durable_manager.record_event(job_id, "writeback_completed", "all 4 stages done")

    def _on_job_stuck(job_id: str, paused_actions: list[str]) -> None:
        # Re-review blocker #3: when no step can make further
        # progress (at least one paused, no pending), transition
        # both ledgers to operator review so the existing
        # /jobs/{id} endpoint reflects the failure state. The
        # recovery bundle endpoint exposes the paused step list
        # so the operator can decide whether to /retry, /resolve,
        # or fix the underlying issue and let the worker resume.
        from bridge.jobs import UNKNOWN as _UNKNOWN

        mem_job = in_memory_manager.get_job(job_id)
        if mem_job is not None and mem_job.status not in ("failed", "aborted", _UNKNOWN):
            mem_job.status = _UNKNOWN
            mem_job.add_event(
                "writeback_stuck",
                f"paused steps: {','.join(paused_actions)}",
            )
        durable_manager.mark_status(
            job_id,
            _UNKNOWN,
            error="; ".join(f"step {a} paused" for a in paused_actions),
        )
        durable_manager.record_event(
            job_id,
            "writeback_stuck",
            f"paused steps: {','.join(paused_actions)}",
        )

    def _on_step_paused(job_id: str, action: str) -> None:
        # Re-review round 5 blocker #4: the worker's exception
        # handler pauses a step on an unknown dispatcher bug and
        # then calls this hook. Cascade-pause the dependent steps
        # (so they don't keep deferring forever on a bug that's
        # in create_experiment) and call _maybe_finish so the
        # job transitions to operator review.
        dispatcher._cascade_pause_dependents(job_id, reason=f"{action} permanently failed")
        dispatcher._maybe_finish(job_id)

    dispatcher = WritebackDispatcher(
        durable_manager,
        elabftw,
        on_step_complete=_on_step_complete,
        on_all_steps_done=_on_all_steps_done,
        on_job_stuck=_on_job_stuck,
    )
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        on_step_paused=_on_step_paused,
        interval_seconds=15.0,
    )
    worker.start()
    return worker


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

    On the production path (``config is not None``), the bridge also
    opens a durable :class:`bridge.durable.manager.JobManager` against
    ``config.bridge_state_dir`` and wires the operator recovery
    endpoints (issue #44).
    """
    if config is None and job_manager is None:
        config = BridgeConfig.from_env()
    manager = job_manager or JobManager()
    token = _bridge_token(config)

    # Open the durable ledger first so the executor can be wired with
    # the manager + step ledger (issue #44). The factory captured below
    # is also used by ``register_writeback_routes`` to mint a fresh
    # per-request manager (each route handler opens and closes its
    # own connection; the request thread does not share the worker's
    # connection to keep WAL contention out of the request hot path).
    durable_manager: DurableJobManager | None = None
    durable_ledger: Any | None = None
    durable_factory: Callable[[], DurableJobManager] | None = None
    if config is not None and config.bridge_state_dir:
        from pathlib import Path

        from bridge.durable.worker import StepLedger as _StepLedger

        state_path = Path(config.bridge_state_dir)
        durable_manager = DurableJobManager(state_path)
        durable_ledger = _StepLedger(durable_manager.conn)

        def make_durable_manager() -> DurableJobManager:
            return DurableJobManager(state_path)

        durable_factory = make_durable_manager

    bridge_executor: BridgeExecutor | None = executor
    elabftw_client: ElabftwClient | None = None
    if config is not None:
        bridge_executor, elabftw_client = _wire_executor(
            config,
            manager,
            durable_manager=durable_manager,
            durable_ledger=durable_ledger,
        )

    # Start the durable writeback worker (issue #44) when configured.
    # The worker reads pending steps from the durable ledger and calls
    # the dispatcher's eLabFTW operations. Idempotency tokens +
    # per-artifact ``uploaded`` flag make every dispatch safely
    # retryable across a process restart.
    durable_worker: Any | None = None
    if durable_manager is not None and durable_ledger is not None and elabftw_client is not None:
        durable_worker = _start_durable_worker(manager, durable_manager, elabftw_client)

    app = FastAPI(
        title="Wallac Victor2 Bridge",
        description="Direct-submit HTTP API for instrument execution",
        version="2.0.0",
    )

    install_security_headers(app)
    _configure_cors(app, config)
    _register_routes(app, manager, token, bridge_executor, durable_manager=durable_manager)

    if durable_factory is not None and config is not None:
        register_writeback_routes(
            app,
            manager_factory=durable_factory,
            token=token or None,
        )

        @app.on_event("shutdown")
        def _close_durable() -> None:  # pragma: no cover (FastAPI lifespan)
            if durable_worker is not None:
                durable_worker.stop()
            if durable_manager is not None:
                durable_manager.close()

    return app
