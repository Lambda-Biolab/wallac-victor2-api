"""Operator recovery endpoints for the durable writeback spool.

Mounted into the bridge FastAPI app at ``/writeback/...``. The
endpoints are auth-gated by the same bearer token the rest of the
bridge uses (the existing ``WALLAC_DESIGNER_TOKEN`` / bridge auth).

Endpoints:

    GET  /writeback                      — full ledger snapshot
    GET  /writeback/{job_id}             — single job view + step status
    POST /writeback/{job_id}/retry       — manually re-enqueue a paused job
    POST /writeback/{job_id}/resolve     — mark an unrecoverable job resolved
    GET  /writeback/{job_id}/recovery-bundle
                                        — diagnostics + artifact list (no secrets)

The recovery bundle never includes API keys, CA private keys, or
service private keys — only public metadata, step status, and artifact
manifest paths. The ``recovery_bundle`` payload is what an operator
uses to manually push a writeback if eLabFTW was offline for the
entire automatic-retry window.

Issue #44 acceptance criteria satisfied:

* Pause, do not retry, on auth/TLS/schema errors (retry policy
  enforces; ``/retry`` re-enqueues only by explicit operator action).
* Operators can inspect pending/partial writebacks and explicitly retry
  or resolve them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from bridge.durable.planner import build_recovery_bundle


def _step_rows(manager: Any, job_id: str) -> dict[str, dict[str, Any]]:
    """Return per-step status rows for a job as ``step_id -> row``."""
    return {
        row["step_id"]: dict(row)
        for row in manager.conn.execute(
            "SELECT step_id, action, status, attempts, "
            "next_attempt_at, completed_at, detail "
            "FROM writeback_steps WHERE job_id = ? ORDER BY step_id",
            (job_id,),
        )
    }


def _make_auth(token: str | None) -> Callable[[str | None], None]:
    """Return an auth-check closure bound to ``token``.

    ``token=None`` disables the check (dev path).
    """
    if token is None:
        return lambda _auth: None

    def _check(authorization: str | None) -> None:
        if not authorization or authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    return _check


def _with_manager(manager_factory: Callable[[], Any], fn: Callable[[Any], Any]) -> Any:
    """Open a manager via ``factory``, run ``fn(manager)``, always close."""
    manager = manager_factory()
    try:
        return fn(manager)
    finally:
        manager.close()


def register_writeback_routes(
    app_or_router: Any,
    *,
    manager_factory: Callable[[], Any],
    token: str | None,
) -> None:
    """Attach the writeback routes to a FastAPI app or APIRouter.

    ``manager_factory`` returns a :class:`bridge.durable.manager.JobManager`
    bound to the live bridge state directory. ``token`` is the bridge
    bearer token; ``None`` disables auth (dev path).
    """
    router = APIRouter(prefix="/writeback", tags=["writeback"])
    check_auth = _make_auth(token)

    @router.get("")
    def snapshot(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_auth(authorization)
        return _with_manager(manager_factory, lambda m: m.snapshot())

    @router.get("/{job_id}")
    def job_view(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_auth(authorization)

        def _view(manager: Any) -> dict[str, Any]:
            job = manager.get_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"job {job_id} not found")
            view = job.to_dict()
            # Per-step status merged in so operators get one round-trip.
            view["writeback_steps"] = _step_rows(manager, job_id)
            return view

        return _with_manager(manager_factory, _view)

    @router.post("/{job_id}/retry")
    def retry_paused(
        job_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Operator-initiated retry.

        Re-enqueues every ``paused`` step on this job and bumps the
        ``next_attempt_at`` to now. The background worker picks them
        up on the next tick. Permanent failures (auth/TLS/schema) still
        pause after this re-enqueue if eLabFTW is still misbehaving —
        this is a retry of the delivery, never of the hardware run.
        """
        check_auth(authorization)

        def _retry(manager: Any) -> dict[str, Any]:
            paused = list(
                manager.conn.execute(
                    "SELECT step_id FROM writeback_steps WHERE job_id = ? AND status = 'paused'",
                    (job_id,),
                )
            )
            if not paused:
                raise HTTPException(
                    status_code=409,
                    detail=f"no paused steps for job {job_id}",
                )
            manager.conn.execute(
                "UPDATE writeback_steps SET status = 'pending', next_attempt_at = NULL "
                "WHERE job_id = ? AND status = 'paused'",
                (job_id,),
            )
            manager.record_event(job_id, "writeback_manual_retry", f"steps={len(paused)}")
            return {
                "job_id": job_id,
                "requeued_steps": [row["step_id"] for row in paused],
            }

        return _with_manager(manager_factory, _retry)

    @router.post("/{job_id}/resolve")
    def resolve(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Mark an unrecoverable job resolved.

        Used when the operator has manually pushed the writeback (e.g.
        uploaded the raw file outside the bridge) and wants the bridge
        to stop trying. The job status is recorded as
        ``resolved_operator`` — distinct from the regular terminal
        ``completed`` — so audit tooling can distinguish automated
        successes from operator-mediated resolutions.
        """
        check_auth(authorization)

        def _resolve(manager: Any) -> dict[str, Any]:
            job = manager.get_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"job {job_id} not found")
            manager.mark_status(job_id, "resolved_operator", error="resolved by operator")
            manager.record_event(job_id, "writeback_resolved", "operator resolved manually")
            return {"job_id": job_id, "status": "resolved_operator"}

        return _with_manager(manager_factory, _resolve)

    @router.get("/{job_id}/recovery-bundle")
    def recovery_bundle(
        job_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Operator-facing recovery payload for a paused job.

        Contains job metadata, event timeline, step status, and the
        artifact manifest (path + SHA-256). NEVER contains API keys,
        private keys, or service tokens.
        """
        check_auth(authorization)

        def _bundle(manager: Any) -> dict[str, Any]:
            return build_recovery_bundle(manager, job_id).__dict__

        try:
            return _with_manager(manager_factory, _bundle)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Mount on the caller's router OR app.
    if hasattr(app_or_router, "include_router"):
        app_or_router.include_router(router)
    else:
        raise TypeError(
            f"register_writeback_routes expects a FastAPI app or APIRouter, "
            f"got {type(app_or_router).__name__}"
        )
