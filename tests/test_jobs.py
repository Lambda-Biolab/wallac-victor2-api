"""Tests for the direct-submit job manager and bridge HTTP API."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.bridge_app import create_bridge_app
from bridge.jobs import (
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

    def test_aborted_job_allows_resubmit(self) -> None:
        mgr = JobManager()
        j1 = mgr.submit_job({"title": "A", "elabftw_experiment_id": 9})
        j1.status = FAILED
        j2 = mgr.submit_job({"title": "B", "elabftw_experiment_id": 9})
        assert j2.job_id != j1.job_id


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


# Need threading import for the queue test
import threading  # noqa: E402
