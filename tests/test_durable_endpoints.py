"""FastAPI tests for the operator recovery endpoints.

Verifies:

* Auth gate (401 when bearer token mismatches).
* Snapshot lists pending and paused steps.
* GET /writeback/{job_id} merges step status.
* POST /retry re-enqueues paused steps.
* POST /resolve records ``resolved_operator`` status without
  clearing the events timeline.
* GET /recovery-bundle is secret-free.

Uses the same ``tempfile.TemporaryDirectory`` state directory as
``test_durable_spool`` so the live bridge state is never touched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridge.durable.endpoints import register_writeback_routes
from bridge.durable.manager import JobManager
from bridge.durable.worker import StepLedger, record_step_outcome


@pytest.fixture
def stack():
    with tempfile.TemporaryDirectory(prefix="wallac-bridge-ep-") as tmp:
        state = Path(tmp)
        m = JobManager(state)

        job_id = (
            m.submit_job(
                job_id="job-test-1",
                title="e2e test",
                execution_mode="existing_protocol",
                protocol_name="Absorbance @ 610 (1.0s)",
                protocol_id=2000008,
                elabftw_experiment_id=42,
                wells_spec={"wells": ["A1"]},
            )["job_id"]
            if False
            else m.submit_job(
                job_id="job-test-1",
                title="e2e test",
                execution_mode="existing_protocol",
                protocol_name="Absorbance @ 610 (1.0s)",
                protocol_id=2000008,
                elabftw_experiment_id=42,
                wells_spec={"wells": ["A1"]},
            ).job_id
        )
        # enqueue a paused step
        ledger = StepLedger(m.conn)
        ledger.enqueue(
            [
                __import__("bridge.durable.worker", fromlist=["PendingStep"]).PendingStep(
                    step_id=f"{job_id}:upload_raw:ep",
                    job_id=job_id,
                    action="upload_raw",
                    idempotency="ep-tok",
                ),
            ]
        )
        record_step_outcome(
            ledger,
            step_id=f"{job_id}:upload_raw:ep",
            http_status=403,
            detail="forbidden",
        )

        app = FastAPI()
        register_writeback_routes(
            app,
            manager_factory=lambda: JobManager(state),
            token="bridge-token-test",
        )
        client = TestClient(app)
        yield {"client": client, "manager": m, "state": state, "job_id": job_id}
        m.close()


def test_snapshot_lists_pending_and_paused(stack) -> None:
    r = stack["client"].get("/writeback", headers={"Authorization": "Bearer bridge-token-test"})
    assert r.status_code == 200
    body = r.json()
    assert any(j["job_id"] == stack["job_id"] for j in body["jobs"])
    assert body["oldest_pending_step"] is None  # only step is paused


def test_unauthorized_request_returns_401(stack) -> None:
    r = stack["client"].get("/writeback")
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_get_job_view_merges_step_status(stack) -> None:
    r = stack["client"].get(
        f"/writeback/{stack['job_id']}",
        headers={"Authorization": "Bearer bridge-token-test"},
    )
    assert r.status_code == 200
    body = r.json()
    step = body["writeback_steps"][f"{stack['job_id']}:upload_raw:ep"]
    assert step["status"] == "paused"
    assert step["attempts"] == 1


def test_retry_re_enqueues_paused_steps(stack) -> None:
    r = stack["client"].post(
        f"/writeback/{stack['job_id']}/retry",
        headers={"Authorization": "Bearer bridge-token-test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["requeued_steps"]) == 1
    # ledger now pending
    row = (
        stack["manager"]
        .conn.execute(
            "SELECT status FROM writeback_steps WHERE job_id = ?",
            (stack["job_id"],),
        )
        .fetchone()
    )
    assert row["status"] == "pending"


def test_resolve_records_status_without_losing_events(stack) -> None:
    r = stack["client"].post(
        f"/writeback/{stack['job_id']}/resolve",
        headers={"Authorization": "Bearer bridge-token-test"},
    )
    assert r.status_code == 200
    job = stack["manager"].get_job(stack["job_id"])
    assert job.status == "resolved_operator"
    events = [e["event"] for e in job.events]
    assert "writeback_resolved" in events
    assert "job_submitted" in events  # timeline preserved


def test_recovery_bundle_omits_secrets(stack) -> None:
    r = stack["client"].get(
        f"/writeback/{stack['job_id']}/recovery-bundle",
        headers={"Authorization": "Bearer bridge-token-test"},
    )
    assert r.status_code == 200
    blob = r.text
    for needle in ("api_key", "API_KEY", "private_key", "BEGIN RSA", ".key"):
        assert needle.lower() not in blob.lower(), f"recovery bundle leaks {needle!r}"


def test_404_on_unknown_job(stack) -> None:
    r = stack["client"].get(
        "/writeback/job-does-not-exist",
        headers={"Authorization": "Bearer bridge-token-test"},
    )
    assert r.status_code == 404


def test_auth_disabled_in_dev(stack) -> None:
    """Pass token=None to register_writeback_routes; no auth required."""
    app = FastAPI()
    register_writeback_routes(
        app,
        manager_factory=lambda: JobManager(stack["state"]),
        token=None,
    )
    client = TestClient(app)
    r = client.get("/writeback")
    assert r.status_code == 200
