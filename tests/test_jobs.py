"""Tests for the direct-submit job manager and bridge HTTP API."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.bridge_app import create_bridge_app
from bridge.jobs import (
    ABORTED,
    ACCEPTED,
    COMPLETED,
    FAILED,
    UNKNOWN,
    DuplicateJobError,
    Job,
    JobManager,
)

# --- JobManager tests ---


class TestJobManager:
    def test_submit_job(self) -> None:
        mgr = JobManager()
        job = mgr.submit_job({"title": "Test", "execution_mode": "existing_protocol"})
        assert job.job_id.startswith("job-")
        assert job.status == ACCEPTED
        assert job.title == "Test"
        assert job.created_at != ""

    def test_get_job(self) -> None:
        mgr = JobManager()
        submitted = mgr.submit_job({"title": "Test"})
        retrieved = mgr.get_job(submitted.job_id)
        assert retrieved is not None
        assert retrieved.job_id == submitted.job_id

    def test_get_job_not_found(self) -> None:
        mgr = JobManager()
        assert mgr.get_job("nonexistent") is None

    def test_list_jobs(self) -> None:
        mgr = JobManager()
        mgr.submit_job({"title": "Job 1", "elabftw_experiment_id": 301})
        mgr.submit_job({"title": "Job 2", "elabftw_experiment_id": 302})
        jobs = mgr.list_jobs()
        assert len(jobs) == 2

    def test_request_abort(self) -> None:
        mgr = JobManager()
        job = mgr.submit_job({"title": "Test"})
        assert mgr.request_abort(job.job_id) is True
        assert job.abort_requested is True

    def test_abort_nonexistent_job(self) -> None:
        mgr = JobManager()
        assert mgr.request_abort("nonexistent") is False

    def test_abort_terminal_job_rejected(self) -> None:
        mgr = JobManager()
        job = mgr.submit_job({"title": "Test"})
        job.status = COMPLETED
        assert mgr.request_abort(job.job_id) is False

    def test_abort_while_queued_skips_executor_and_marks_aborted(self) -> None:
        """A job aborted while still accepted/queued must transition directly
        to aborted without invoking the executor — the documented
        accepted -> aborted contract (docs/abort-recovery.md). No physical
        work may be started."""
        mgr = JobManager()
        executed: list[str] = []

        def executor(job: Job) -> None:
            executed.append(job.job_id)
            job.status = COMPLETED
            job.add_event("done")

        mgr.set_executor(executor)
        try:
            job = mgr.submit_job({"title": "Test"})
            # Request abort before the worker can pick the job up.
            assert mgr.request_abort(job.job_id) is True

            mgr.start_worker()
            for _ in range(50):
                if job.status in {COMPLETED, FAILED, ABORTED, UNKNOWN}:
                    break
                time.sleep(0.1)

            assert job.status == ABORTED
            # The executor must never have run — no hardware was started.
            assert executed == []
            assert job.started_at == ""
            assert any(e["event"] == "execution_aborted" for e in job.events)
        finally:
            mgr.stop_worker()

    def test_worker_executes_job(self) -> None:
        mgr = JobManager()
        executed: list[str] = []

        def executor(job: Job) -> None:
            executed.append(job.job_id)
            job.status = COMPLETED
            job.add_event("done")

        mgr.set_executor(executor)
        mgr.start_worker()
        try:
            job = mgr.submit_job({"title": "Test"})
            # Wait for execution
            for _ in range(50):
                if job.status in {COMPLETED, FAILED, UNKNOWN}:
                    break
                time.sleep(0.1)
            assert job.job_id in executed
            assert job.status == COMPLETED
        finally:
            mgr.stop_worker()

    def test_worker_queues_jobs(self) -> None:
        mgr = JobManager()
        executed: list[str] = []
        barrier = threading.Event()

        def executor(job: Job) -> None:
            barrier.wait(timeout=5.0)
            executed.append(job.job_id)
            job.status = COMPLETED

        mgr.set_executor(executor)
        mgr.start_worker()
        try:
            job1 = mgr.submit_job({"title": "Job 1", "elabftw_experiment_id": 101})
            job2 = mgr.submit_job({"title": "Job 2", "elabftw_experiment_id": 102})
            # Release both
            barrier.set()
            # Wait
            for _ in range(50):
                if job1.status == COMPLETED and job2.status == COMPLETED:
                    break
                time.sleep(0.1)
            assert job1.job_id in executed
            assert job2.job_id in executed
            # Job 1 should execute before Job 2
            assert executed.index(job1.job_id) < executed.index(job2.job_id)
        finally:
            mgr.stop_worker()

    def test_worker_handles_executor_error(self) -> None:
        mgr = JobManager()

        def bad_executor(job: Job) -> None:
            raise RuntimeError("Boom")

        mgr.set_executor(bad_executor)
        mgr.start_worker()
        try:
            job = mgr.submit_job({"title": "Test"})
            for _ in range(50):
                if job.status in {COMPLETED, FAILED, UNKNOWN}:
                    break
                time.sleep(0.1)
            assert job.status == UNKNOWN
            assert "Boom" in job.error
        finally:
            mgr.stop_worker()

    def test_worker_no_executor(self) -> None:
        mgr = JobManager()
        mgr.start_worker()
        try:
            job = mgr.submit_job({"title": "Test"})
            for _ in range(50):
                if job.status in {COMPLETED, FAILED, UNKNOWN}:
                    break
                time.sleep(0.1)
            assert job.status == FAILED
            assert "No executor" in job.error
        finally:
            mgr.stop_worker()

    def test_current_job(self) -> None:
        mgr = JobManager()
        barrier = threading.Event()

        def executor(job: Job) -> None:
            barrier.wait(timeout=5.0)
            job.status = COMPLETED

        mgr.set_executor(executor)
        mgr.start_worker()
        try:
            job = mgr.submit_job({"title": "Test"})
            # Wait for job to start
            for _ in range(50):
                if mgr.current_job is not None:
                    break
                time.sleep(0.1)
            assert mgr.current_job is not None
            assert mgr.current_job.job_id == job.job_id

            barrier.set()
            for _ in range(50):
                if mgr.current_job is None:
                    break
                time.sleep(0.1)
            assert mgr.current_job is None
        finally:
            mgr.stop_worker()

    def test_job_to_dict(self) -> None:
        job = Job(
            job_id="job-test",
            title="Test",
            execution_mode="existing_protocol",
            protocol_name="Absorbance @ 600",
        )
        d = job.to_dict()
        assert d["job_id"] == "job-test"
        assert d["title"] == "Test"
        assert d["status"] == ACCEPTED
        assert d["events"] == []

    def test_job_add_event(self) -> None:
        job = Job(job_id="test", title="T", execution_mode="existing_protocol")
        job.add_event("test_event", "detail")
        assert len(job.events) == 1
        assert job.events[0]["event"] == "test_event"
        assert job.events[0]["detail"] == "detail"
        assert job.events[0]["ts"] != ""


# --- HIGH: accepted -> aborted race (atomic transition) ---


class TestAbortRaceAtomicity:
    """Regression for the HIGH abort race in ``JobManager._execute_job``.

    The accepted -> aborted contract (no physical work) requires the abort
    check and the RUNNING transition to be atomic with ``request_abort``.

    The race is deterministic here because the worker thread is gated after
    the queue pick (so it has NOT yet committed to RUNNING) and the abort is
    issued from the test thread while the worker is gated — placing the abort
    precisely in the window the old non-atomic check-then-set left open. The
    fixed atomic block then observes ``abort_requested`` and goes aborted
    without invoking the executor. A regression that hoists the check out of
    the lock and sets RUNNING outside it would let the executor run here.
    """

    def test_abort_in_transition_window_is_honored_without_executor(self) -> None:
        mgr = JobManager()
        executed: list[str] = []

        def executor(job: Job) -> None:
            executed.append(job.job_id)
            job.status = COMPLETED
            job.add_event("done")

        mgr.set_executor(executor)
        job = mgr.submit_job({"title": "Race"})
        mgr._current_job = job  # mimic _worker_loop having popped the job

        release = threading.Event()
        started = threading.Event()

        def gated_worker() -> None:
            started.set()
            release.wait(timeout=5.0)
            mgr._execute_job(job.job_id, job)

        worker = threading.Thread(target=gated_worker, daemon=True)
        worker.start()
        started.wait(timeout=5.0)

        # Worker has not entered _execute_job yet (gated) and the job is
        # still accepted: issue the abort in the race window.
        assert job.status == ACCEPTED
        assert mgr.request_abort(job.job_id) is True
        assert job.abort_requested is True

        release.set()
        worker.join(timeout=5.0)

        assert job.status == ABORTED, job.events
        # No physical work may be started for an abort requested while accepted.
        assert executed == []
        assert job.started_at == ""
        assert any(e["event"] == "execution_aborted" for e in job.events)
        assert mgr.current_job is None
        assert job.completed_at != ""

    def test_abort_intent_is_visible_before_waiting_for_run_start_lock(self) -> None:
        mgr = JobManager()
        executed: list[str] = []

        def executor(job: Job) -> None:
            executed.append(job.job_id)

        mgr.set_executor(executor)
        job = mgr.submit_job({"title": "Abort intent ordering"})
        mgr._current_job = job
        abort_returned = threading.Event()

        def request_abort() -> None:
            mgr.request_abort(job.job_id)
            abort_returned.set()

        with job._run_start_lock:
            abort_thread = threading.Thread(target=request_abort, daemon=True)
            abort_thread.start()
            for _ in range(100):
                if job.abort_requested:
                    break
                time.sleep(0.001)
            assert job.abort_requested is True
            assert abort_returned.is_set() is False

            mgr._execute_job(job.job_id, job)

        abort_thread.join(timeout=5.0)

        assert abort_returned.is_set()
        assert job.status == ABORTED
        assert job.started_at == ""
        assert executed == []
        assert not any(event["event"] == "execution_started" for event in job.events)


# --- MEDIUM: no-executor terminal exit cleanup ---


class TestNoExecutorCleanup:
    def test_no_executor_exit_clears_current_job_and_records_completed_at(
        self,
    ) -> None:
        """A pre-executor terminal exit must clean up _current_job and set
        completed_at like every other terminal path, so consumers never
        observe a stale current job or a missing completion timestamp."""
        mgr = JobManager()
        job = mgr.submit_job({"title": "NoExec"})
        mgr._current_job = job  # mimic _worker_loop having popped the job

        mgr._execute_job(job.job_id, job)

        assert job.status == FAILED
        assert "No executor" in (job.error or "")
        assert job.completed_at != "", "completed_at must be set on terminal exit"
        assert any(e["event"] == "execution_failed" for e in job.events)
        assert mgr.current_job is None, "_current_job must be cleared on terminal exit"


# --- Bridge HTTP API tests ---


@pytest.fixture
def job_manager() -> JobManager:
    return JobManager()


@pytest.fixture
def app(job_manager: JobManager) -> Any:
    return create_bridge_app(job_manager=job_manager)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


class TestBridgeApp:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_submit_job(self, client: TestClient) -> None:
        r = client.post(
            "/jobs",
            json={
                "title": "OD600 Test",
                "execution_mode": "existing_protocol",
                "protocol_name": "Absorbance @ 600 (1.0s)",
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["job_id"].startswith("job-")
        assert data["status"] == ACCEPTED
        assert data["title"] == "OD600 Test"

    def test_get_job(self, client: TestClient) -> None:
        r = client.post("/jobs", json={"title": "Test", "execution_mode": "existing_protocol"})
        job_id = r.json()["job_id"]
        r2 = client.get(f"/jobs/{job_id}")
        assert r2.status_code == 200
        assert r2.json()["job_id"] == job_id

    def test_get_job_not_found(self, client: TestClient) -> None:
        r = client.get("/jobs/nonexistent")
        assert r.status_code == 404

    def test_list_jobs(self, client: TestClient) -> None:
        client.post("/jobs", json={"title": "Job 1", "elabftw_experiment_id": 201})
        client.post("/jobs", json={"title": "Job 2", "elabftw_experiment_id": 202})
        r = client.get("/jobs")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_abort_job(self, client: TestClient) -> None:
        r = client.post("/jobs", json={"title": "Test", "execution_mode": "existing_protocol"})
        job_id = r.json()["job_id"]
        r2 = client.post(f"/jobs/{job_id}/abort")
        assert r2.status_code == 200
        assert r2.json()["abort_requested"] is True

    def test_abort_not_found(self, client: TestClient) -> None:
        r = client.post("/jobs/nonexistent/abort")
        assert r.status_code == 409

    def test_duplicate_elabftw_experiment_rejected(self, client: TestClient) -> None:
        """Two submissions with the same elabftw_experiment_id are treated as
        duplicates while the first is still active."""
        r1 = client.post(
            "/jobs",
            json={"title": "Run 1", "elabftw_experiment_id": 42},
        )
        assert r1.status_code == 201
        r2 = client.post(
            "/jobs",
            json={"title": "Run 2", "elabftw_experiment_id": 42},
        )
        assert r2.status_code == 409
        body = r2.json()
        assert body["detail"]["existing_job_id"] == r1.json()["job_id"]

    def test_duplicate_after_terminal_allowed(self, client: TestClient) -> None:
        """Resubmitting the same experiment after the first job completed
        is allowed (terminal jobs don't count as duplicates)."""
        client.post(
            "/jobs",
            json={"title": "Run 1", "elabftw_experiment_id": 99},
        )
        # Without an executor wired, the first job stays "accepted".
        # A second submit with the same experiment_id must be rejected.
        r2 = client.post(
            "/jobs",
            json={"title": "Run 2", "elabftw_experiment_id": 99},
        )
        assert r2.status_code == 409

    def test_duplicate_spec_hash_rejected(self, client: TestClient) -> None:
        """Two submissions with the same method/layout refs but no
        elabftw_experiment_id are detected via content hash."""
        spec = {
            "title": "Run",
            "execution_mode": "generated_protocol",
            "method_ref": {"object_id": 1},
            "layout_ref": {"object_id": 2},
        }
        r1 = client.post("/jobs", json=spec)
        assert r1.status_code == 201
        # Same refs, different title — still a duplicate
        spec2 = dict(spec, title="Different title")
        r2 = client.post("/jobs", json=spec2)
        assert r2.status_code == 409
        assert r2.json()["detail"]["existing_job_id"] == r1.json()["job_id"]

    def test_different_specs_both_accepted(self, client: TestClient) -> None:
        """Two submissions with genuinely different specs are both accepted."""
        r1 = client.post(
            "/jobs",
            json={"title": "A", "elabftw_experiment_id": 501},
        )
        r2 = client.post(
            "/jobs",
            json={"title": "B", "elabftw_experiment_id": 502},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["job_id"] != r2.json()["job_id"]


# --- JobManager-level duplicate detection tests ---


class TestDuplicateDetection:
    def test_same_experiment_id_raises(self) -> None:
        mgr = JobManager()
        mgr.submit_job({"title": "A", "elabftw_experiment_id": 7})
        with pytest.raises(DuplicateJobError) as exc:
            mgr.submit_job({"title": "B", "elabftw_experiment_id": 7})
        assert exc.value.existing_job_id

    def test_completed_job_allows_resubmit(self) -> None:
        """A terminal job does not block resubmission with the same key."""
        mgr = JobManager()
        j1 = mgr.submit_job({"title": "A", "elabftw_experiment_id": 8})
        j1.status = COMPLETED
        # Should not raise
        j2 = mgr.submit_job({"title": "B", "elabftw_experiment_id": 8})
        assert j2.job_id != j1.job_id

    def test_different_protocol_ids_are_not_duplicates(self) -> None:
        mgr = JobManager()
        first = mgr.submit_job(
            {
                "title": "A",
                "execution_mode": "existing_protocol",
                "protocol_id": 101,
                "protocol_name": "Shared name",
            }
        )
        second = mgr.submit_job(
            {
                "title": "B",
                "execution_mode": "existing_protocol",
                "protocol_id": 102,
                "protocol_name": "Shared name",
            }
        )

        assert second.job_id != first.job_id

    def test_protocol_id_precedence_deduplicates_different_names(self) -> None:
        mgr = JobManager()
        first = mgr.submit_job(
            {
                "title": "A",
                "execution_mode": "existing_protocol",
                "protocol_id": 101,
                "protocol_name": "Stale client name",
            }
        )

        with pytest.raises(DuplicateJobError) as exc:
            mgr.submit_job(
                {
                    "title": "B",
                    "execution_mode": "existing_protocol",
                    "protocol_id": 101,
                    "protocol_name": "Current instrument name",
                }
            )

        assert exc.value.existing_job_id == first.job_id

    def test_aborted_job_allows_resubmit(self) -> None:
        mgr = JobManager()
        j1 = mgr.submit_job({"title": "A", "elabftw_experiment_id": 9})
        j1.status = FAILED
        j2 = mgr.submit_job({"title": "B", "elabftw_experiment_id": 9})
        assert j2.job_id != j1.job_id

    def test_generated_protocol_ignores_protocol_id_and_name(self) -> None:
        """generated_protocol dedup keys must ignore protocol_id and
        protocol_name — only the signed refs matter."""
        mgr = JobManager()
        spec_a = {
            "title": "A",
            "execution_mode": "generated_protocol",
            "protocol_id": 999,
            "protocol_name": "Generated Alpha",
            "method_ref": {"object_id": 1},
            "layout_ref": {"object_id": 2},
        }
        spec_b = {
            "title": "B",
            "execution_mode": "generated_protocol",
            "protocol_id": 777,
            "protocol_name": "Generated Beta",
            "method_ref": {"object_id": 1},
            "layout_ref": {"object_id": 2},
        }

        mgr.submit_job(spec_a)
        with pytest.raises(DuplicateJobError):
            mgr.submit_job(spec_b)

    def test_existing_protocol_deduplicates_by_name_when_no_id(self) -> None:
        """existing_protocol without protocol_id falls back to name-based
        dedup — same name, same refs => duplicate."""
        mgr = JobManager()
        spec = {
            "title": "Run",
            "execution_mode": "existing_protocol",
            "protocol_name": "Absorbance @ 600 nm",
            "method_ref": {"object_id": 10},
            "layout_ref": {"object_id": 20},
        }
        mgr.submit_job(dict(spec, title="First"))
        with pytest.raises(DuplicateJobError):
            mgr.submit_job(dict(spec, title="Second"))

    def test_existing_protocol_different_names_no_id_are_not_duplicates(self) -> None:
        """existing_protocol without protocol_id: different names => different
        dedup keys, both accepted."""
        mgr = JobManager()
        common = {
            "execution_mode": "existing_protocol",
            "method_ref": {"object_id": 10},
            "layout_ref": {"object_id": 20},
        }
        first = mgr.submit_job(dict(common, title="A", protocol_name="Protocol One"))
        second = mgr.submit_job(dict(common, title="B", protocol_name="Protocol Two"))
        assert second.job_id != first.job_id


# --- Auth tests ---


@pytest.fixture
def authed_app(job_manager: JobManager, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("WALLAC_BRIDGE_TOKEN", "secret-token")
    return create_bridge_app(job_manager=job_manager)


@pytest.fixture
def authed_client(authed_app: Any) -> TestClient:
    return TestClient(authed_app)


class TestBridgeAppAuth:
    def test_no_token_required_by_default(self, client: TestClient) -> None:
        r = client.get("/jobs")
        assert r.status_code == 200

    def test_token_required_when_set(self, authed_client: TestClient) -> None:
        r = authed_client.get("/jobs")
        assert r.status_code == 401

    def test_valid_token(self, authed_client: TestClient) -> None:
        r = authed_client.get("/jobs", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200

    def test_invalid_token(self, authed_client: TestClient) -> None:
        r = authed_client.get("/jobs", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
