"""Tests for the BridgeExecutor — direct-submit execution with hash verification.

C20 requires reference hash verification in the executor path.
Tests cover:
- Valid hash-bound ref with the correct hash passes verification
- Valid ref executes through existing generated_protocol path (dry-run)
- Hash mismatch blocks execution (fails closed)
- Missing hash in ref blocks execution (fails closed)
- Missing object_id/attachment_id blocks execution (fails closed)
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from bridge.canonical import canonicalize_and_hash
from bridge.executor import BridgeExecutor
from bridge.jobs import Job, JobManager
from bridge.vm_agent_client import VmAgentError

# --- Mock clients ---


class MockElabftwClient:
    """In-memory mock for eLabFTW client in executor tests."""

    def __init__(self) -> None:
        self._uploads: dict[tuple[int, int], bytes] = {}
        self._experiments: dict[int, dict[str, Any]] = {}
        self._next_exp_id = 1
        self.fail_upload = False
        self.uploaded_files: list[str] = []

    def add_upload(self, item_id: int, upload_id: int, content: bytes) -> None:
        self._uploads[(item_id, upload_id)] = content

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        return self._uploads.get((item_id, upload_id), b"")

    def create_experiment(self, title: str, body: str = "") -> int:
        eid = self._next_exp_id
        self._next_exp_id += 1
        self._experiments[eid] = {"title": title, "body": body}
        return eid

    def upload_experiment_file(
        self, exp_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        if self.fail_upload:
            raise RuntimeError("upload unavailable")
        self.uploaded_files.append(filename)
        return {"id": exp_id, "real_name": filename}

    def patch_experiment(self, exp_id: int, data: dict[str, Any]) -> None:
        self._experiments[exp_id].update(data)


class MockVmAgentClient:
    """Mock vm-agent for executor tests."""

    def __init__(self) -> None:
        self._protocols: list[dict[str, Any]] = []
        self._runs: dict[str, dict[str, Any]] = {}
        self._next_run = 1
        self.job_wells = [
            {"well": "A01", "od": 0.078, "counts": 684016},
            {"well": "A02", "od": 0.089, "counts": 666875},
        ]
        self.fail_clone = False
        self.fail_plate_map = False
        self.protocol_lookup_404 = False
        self.run_states: list[str] = []
        self.live_batches: list[list[dict[str, Any]]] = []
        self.abort_425_remaining = 0
        self.abort_permanent_error: VmAgentError | None = None
        self.abort_calls: list[str] = []
        self.deleted_protocols: list[int] = []
        self.fail_get_jobs = False
        self.jobs = [
            {
                "assay_id": 200,
                "protocol_name": "Absorbance @ 600 (1.0s)",
                "protocol_id": 1001,
            }
        ]
        self.requested_job_ids: list[int] = []

    def add_protocol(self, proto: dict[str, Any]) -> None:
        self._protocols.append(proto)

    def get_protocols(self, refresh: bool = False) -> list[dict[str, Any]]:
        return self._protocols

    def get_protocol(self, name_or_id: str | int) -> dict[str, Any]:
        if self.protocol_lookup_404:
            raise VmAgentError("not found", status_code=404)
        for p in self._protocols:
            if p.get("name") == name_or_id or p.get("id") == name_or_id:
                return p
        raise RuntimeError(f"Protocol {name_or_id} not found")

    def start_run(
        self, protocol: str | int, plate_id: str = "", dry_run: bool = False
    ) -> dict[str, Any]:
        run_id = f"r-{self._next_run:06d}"
        self._next_run += 1
        self._runs[run_id] = {"run_id": run_id, "state": "running"}
        protocol_name = next(
            (
                str(candidate.get("name", ""))
                for candidate in self._protocols
                if str(candidate.get("id")) == str(protocol)
            ),
            f"protocol_{protocol}",
        )
        self.jobs.append(
            {
                "assay_id": max((entry["assay_id"] for entry in self.jobs), default=0) + 1,
                "protocol_name": protocol_name,
                "protocol_id": protocol,
            }
        )
        return {"run_id": run_id, "state": "running", "protocol_id": protocol}

    def get_run(self, run_id: str) -> dict[str, Any]:
        if self.run_states:
            return {"run_id": run_id, "state": self.run_states.pop(0)}
        run = self._runs.get(run_id, {})
        if run.get("state") == "running":
            run["state"] = "measured"
        return run

    def get_run_results(
        self, run_id: str, shape: str = "list", value: str = "od", dedup: bool = True
    ) -> dict[str, Any]:
        if self.live_batches:
            return {"run_id": run_id, "wells": self.live_batches.pop(0)}
        return {"run_id": run_id, "well_count": 0, "wells": []}

    def get_jobs(self) -> list[dict[str, Any]]:
        """Return jobs list — needed by _fetch_and_writeback for assay_id resolution."""
        if self.fail_get_jobs:
            raise VmAgentError("jobs unavailable", status_code=503)
        return list(self.jobs)

    def get_job_results(
        self, job_id: int, shape: str = "list", value: str = "od", dedup: bool = True
    ) -> dict[str, Any]:
        """Return mock wells with OD values — simulates MDB flush complete."""
        self.requested_job_ids.append(job_id)
        return {"wells": list(self.job_wells)}

    def clone_protocol(self, template_id: int, new_id: int, name: str) -> None:
        if self.fail_clone:
            raise RuntimeError("clone failed")

    def update_plate_map(self, protocol_id: int, wells: list[str]) -> None:
        if self.fail_plate_map:
            raise RuntimeError("plate map failed")

    def delete_protocol(self, protocol_id: int) -> None:
        self.deleted_protocols.append(protocol_id)

    def abort_run(self, run_id: str) -> None:
        self.abort_calls.append(run_id)
        if self.abort_permanent_error is not None:
            raise self.abort_permanent_error
        if self.abort_425_remaining:
            self.abort_425_remaining -= 1
            raise VmAgentError("too early", status_code=425)


# --- Fixtures ---


@pytest.fixture
def elabftw() -> MockElabftwClient:
    return MockElabftwClient()


@pytest.fixture
def vm_agent() -> MockVmAgentClient:
    agent = MockVmAgentClient()
    # Add a factory preset protocol so protocol matching works
    agent.add_protocol(
        {
            "id": 1001,
            "name": "Absorbance @ 600 (1.0s)",
            "factory_preset": True,
        }
    )
    return agent


@pytest.fixture
def executor(elabftw: MockElabftwClient, vm_agent: MockVmAgentClient) -> BridgeExecutor:
    return BridgeExecutor(
        vm_agent=vm_agent,
        elabftw=elabftw,
        dry_run=True,  # default to dry-run so we only test validation
    )


@pytest.fixture
def executor_wet(elabftw: MockElabftwClient, vm_agent: MockVmAgentClient) -> BridgeExecutor:
    """Executor with dry_run=False for full execution path tests."""
    return BridgeExecutor(
        vm_agent=vm_agent,
        elabftw=elabftw,
        dry_run=False,
    )


# --- Test: Full generated_protocol execution with hash verification ---


class TestGeneratedProtocolHashVerification:
    """Integration-style tests verifying hash mismatch blocks execution end-to-end."""

    def _make_method_spec(self) -> tuple[dict[str, Any], bytes, str]:
        spec = {
            "schema_name": "wallac.method",
            "schema_version": 1,
            "mode": "photometry",
            "name": "OD600",
            "plate_type": "96-well",
            "photometry": {
                "filter_id": "P610",
                "filter_name": "610nm",
                "read_time_seconds": 1.0,
            },
        }
        spec_bytes, spec_hash = canonicalize_and_hash(spec)
        return spec, spec_bytes, spec_hash

    def _make_job(
        self,
        method_ref: dict[str, Any],
        dry_run: bool = True,
    ) -> Job:
        return Job(
            job_id="test-job-001",
            title="Test Job",
            execution_mode="generated_protocol",
            method_ref=method_ref,
            # To keep things simple in the test, we don't need layout/analysis
            # for the hash verification test -- we just need method_ref to be
            # checked.
            created_at="2025-01-01T00:00:00",
        )

    def _execute_method_spec(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        spec: dict[str, Any],
    ) -> Job:
        spec_bytes, spec_hash = canonicalize_and_hash(spec)
        elabftw.add_upload(42, 5001, spec_bytes)
        job = self._make_job(
            {"object_id": 42, "hash": spec_hash, "json_attachment_id": 5001},
            dry_run=False,
        )
        executor(job)
        return job

    @pytest.mark.parametrize(
        ("ref", "error_fragment"),
        [
            ({"hash": "a" * 64, "json_attachment_id": 5001}, "object_id"),
            ({"object_id": 42, "hash": "a" * 64}, "attachment_id"),
        ],
    )
    def test_incomplete_ref_blocks_execution(
        self,
        executor_wet: BridgeExecutor,
        ref: dict[str, Any],
        error_fragment: str,
    ) -> None:
        job = self._make_job(ref, dry_run=False)

        executor_wet(job)

        assert job.status == "failed"
        assert error_fragment in job.error

    def test_valid_ref_executes(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A valid hash-bound ref passes verification and proceeds to execution.

        For generated_protocol mode, the executor validates method_ref hash
        before attempting any protocol matching. With dry_run=False but a
        matching protocol available, it should proceed to protocol cloning
        and run start.
        """
        _method_spec, method_bytes, method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)

        ref = {
            "object_id": 42,
            "hash": method_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=False)

        # Execute — the mock has a matching protocol so the full path runs.
        executor_wet(job)

        # The job should complete because hash verification passes and the
        # mock vm-agent has a matching protocol. The key assertion is that
        # hash verification did NOT block execution.
        assert job.status == "completed"
        assert "Failed to download specs" not in job.error
        assert "Hash mismatch" not in job.error
        # The spec download should have succeeded
        assert any("specs_downloaded" in e["event"] for e in job.events)
        assert job.assay_prot_id == 201
        assert vm_agent.requested_job_ids == [201]

    def test_legacy_attachment_id_ref_executes(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """The documented legacy attachment_id alias remains supported."""
        _method_spec, method_bytes, method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)
        job = self._make_job(
            {
                "object_id": 42,
                "hash": method_hash,
                "attachment_id": 5001,
            },
            dry_run=False,
        )

        executor_wet(job)

        assert job.status == "completed"
        assert any("specs_downloaded" in event["event"] for event in job.events)

    def test_assay_snapshot_failure_blocks_run_start(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        _method_spec, method_bytes, method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)
        vm_agent.fail_get_jobs = True
        job = self._make_job(
            {
                "object_id": 42,
                "hash": method_hash,
                "json_attachment_id": 5001,
            },
            dry_run=False,
        )

        executor_wet(job)

        assert job.status == "failed"
        assert vm_agent._runs == {}
        assert vm_agent.requested_job_ids == []
        assert any(event["event"] == "assay_snapshot_failed" for event in job.events)

    def test_hash_mismatch_blocks_execution(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Hash mismatch blocks execution — job is failed with appropriate error."""
        _method_spec, method_bytes, _method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)

        # Use a wrong hash
        wrong_hash = "a" * 64

        ref = {
            "object_id": 42,
            "hash": wrong_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=False)

        executor_wet(job)

        assert job.status == "failed"
        assert "Failed to download specs" in job.error
        assert "Hash mismatch" in job.error or "hash mismatch" in job.error
        # Verify the event log captures the failure
        assert any("execution_failed" in e["event"] for e in job.events)

    def test_missing_hash_blocks_execution(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Missing hash in ref blocks execution — job is failed."""
        _method_spec, method_bytes, _method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)

        # Ref without hash (but with valid object_id and attachment_id)
        ref = {
            "object_id": 42,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=False)

        executor_wet(job)

        assert job.status == "failed"
        assert "Failed to download specs" in job.error
        # Should mention missing hash
        assert "hash" in job.error.lower()

    def test_dry_run_valid_ref_passes(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Dry-run with valid ref completes successfully (validation only)."""
        _method_spec, method_bytes, method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)

        ref = {
            "object_id": 42,
            "hash": method_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=True)

        executor(job)

        assert job.status == "completed"
        assert any("dry_run_complete" in e["event"] for e in job.events)

    def test_dry_run_hash_mismatch_fails(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Dry-run with hash mismatch still fails closed (pre-execution check)."""
        _method_spec, method_bytes, _method_hash = self._make_method_spec()
        elabftw.add_upload(42, 5001, method_bytes)

        wrong_hash = "b" * 64
        ref = {
            "object_id": 42,
            "hash": wrong_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=True)

        executor(job)

        assert job.status == "failed"
        assert "Hash mismatch" in job.error or "hash mismatch" in job.error
        assert not any("dry_run_complete" in e["event"] for e in job.events)

    def test_unknown_filter_fails_before_instrument_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        spec, _, _ = self._make_method_spec()
        spec["photometry"]["filter_id"] = "P999"

        job = self._execute_method_spec(executor_wet, elabftw, spec)

        assert job.status == "failed"
        assert "Could not match method spec" in job.error

    def test_writeback_failure_marks_job_failed(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        spec, _, _ = self._make_method_spec()
        elabftw.fail_upload = True

        job = self._execute_method_spec(executor_wet, elabftw, spec)

        assert job.status == "failed"
        assert "Write-back failed" in job.error
        assert any(event["event"] == "writeback_failed" for event in job.events)

    def test_zero_reading_is_preserved_in_results_html(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        spec, _, _ = self._make_method_spec()
        vm_agent.job_wells = [{"well": "A01", "primary_value": "", "od": 0.0, "counts": 999}]

        job = self._execute_method_spec(executor_wet, elabftw, spec)

        assert job.status == "completed"
        body = elabftw._experiments[job.elabftw_experiment_id]["body"]
        assert "Wells measured:</td><td>1" in body
        assert "0.000" in body
        assert "999.000" not in body

    def test_clone_failure_falls_back_to_factory_protocol(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        spec, _, _ = self._make_method_spec()
        vm_agent.fail_clone = True
        layout = {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": [{"well_name": "A1", "role": "measured"}],
        }
        layout_bytes, layout_hash = canonicalize_and_hash(layout)
        elabftw.add_upload(43, 5002, layout_bytes)
        method_bytes, method_hash = canonicalize_and_hash(spec)
        elabftw.add_upload(42, 5001, method_bytes)
        job = self._make_job(
            {"object_id": 42, "hash": method_hash, "json_attachment_id": 5001},
            dry_run=False,
        )
        job.layout_ref = {
            "object_id": 43,
            "hash": layout_hash,
            "json_attachment_id": 5002,
        }

        executor_wet(job)

        assert job.status == "completed"
        assert any(event["event"] == "protocol_clone_failed" for event in job.events)

    def test_abort_request_stops_writeback(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """An abort requested before the run starts must skip physical work
        and writeback entirely. This is the accepted→aborted contract from
        docs/abort-recovery.md — the run is never started, so there is no
        run to abort on the instrument and no results to write back."""
        spec, _, _ = self._make_method_spec()
        method_bytes, method_hash = canonicalize_and_hash(spec)
        elabftw.add_upload(42, 5001, method_bytes)
        job = self._make_job(
            {"object_id": 42, "hash": method_hash, "json_attachment_id": 5001},
            dry_run=False,
        )
        job.abort_requested = True

        executor_wet(job)

        assert job.status == "aborted"
        # No physical run was started, so there is nothing to abort.
        assert not vm_agent._runs
        assert vm_agent.abort_calls == []
        assert job.run_id == ""
        # Writeback never triggered — no results to persist.
        assert elabftw.uploaded_files == []


@pytest.mark.parametrize(
    "job",
    [
        Job(job_id="unknown", title="Unknown", execution_mode="unsupported"),
        Job(job_id="existing", title="Existing", execution_mode="existing_protocol"),
        Job(job_id="generated", title="Generated", execution_mode="generated_protocol"),
    ],
)
def test_executor_rejects_incomplete_or_unknown_jobs(
    executor_wet: BridgeExecutor,
    job: Job,
) -> None:
    executor_wet(job)

    assert job.status == "failed"
    assert job.error
    assert any(event["event"] == "execution_failed" for event in job.events)


class TestRunStartAbortOrdering:
    def test_abort_accepted_before_start_skips_physical_run(
        self,
        executor_wet: BridgeExecutor,
        vm_agent: MockVmAgentClient,
    ) -> None:
        manager = JobManager()
        job = manager.submit_job(
            {
                "title": "Abort before start",
                "execution_mode": "existing_protocol",
                "protocol_id": 1001,
            }
        )
        job.status = "running"

        assert manager.request_abort(job.job_id) is True
        assert executor_wet._start_job_run(job, 1001) == ""

        assert job.status == "aborted"
        assert vm_agent._runs == {}
        assert not any(event["event"] == "run_started" for event in job.events)

    def test_abort_waits_for_in_flight_start_to_publish_run_id(
        self,
        executor_wet: BridgeExecutor,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = JobManager()
        job = manager.submit_job(
            {
                "title": "Start wins",
                "execution_mode": "existing_protocol",
                "protocol_id": 1001,
            }
        )
        job.status = "running"
        start_entered = threading.Event()
        release_start = threading.Event()
        abort_attempted = threading.Event()
        abort_returned = threading.Event()
        original_start = vm_agent.start_run

        def blocking_start(protocol: str | int) -> dict[str, Any]:
            start_entered.set()
            assert release_start.wait(timeout=5.0)
            return original_start(protocol)

        monkeypatch.setattr(vm_agent, "start_run", blocking_start)
        start_thread = threading.Thread(
            target=executor_wet._start_job_run,
            args=(job, 1001),
            daemon=True,
        )

        def request_abort() -> None:
            abort_attempted.set()
            manager.request_abort(job.job_id)
            abort_returned.set()

        start_thread.start()
        assert start_entered.wait(timeout=5.0)
        abort_thread = threading.Thread(target=request_abort, daemon=True)
        abort_thread.start()
        assert abort_attempted.wait(timeout=5.0)
        assert not abort_returned.wait(timeout=0.05)

        release_start.set()
        start_thread.join(timeout=5.0)
        abort_thread.join(timeout=5.0)

        assert abort_returned.is_set()
        assert job.run_id
        assert job.abort_requested is True
        assert any(event["event"] == "run_started" for event in job.events)


class TestAbortFailureSemantics:
    """Regression tests for the documented abort-failure contract
    (docs/abort-recovery.md): a permanent abort failure must be reported as
    ``failed`` (the instrument did not respond), never as a successful
    ``aborted``. Subsequent run completion must not flip it to aborted or
    suppress writeback as if the abort had succeeded."""

    def _make_aborted_job(
        self,
        elabftw: MockElabftwClient,
    ) -> tuple[Job, dict[str, Any], str, str]:
        spec = {
            "schema_name": "wallac.method",
            "schema_version": 1,
            "mode": "photometry",
            "name": "OD600",
            "plate_type": "96-well",
            "photometry": {
                "filter_id": "P610",
                "filter_name": "610nm",
                "read_time_seconds": 1.0,
            },
        }
        spec_bytes, spec_hash = canonicalize_and_hash(spec)
        elabftw.add_upload(42, 5001, spec_bytes)
        ref = {
            "object_id": 42,
            "hash": spec_hash,
            "json_attachment_id": 5001,
        }
        job = Job(
            job_id="test-job-abort-fail",
            title="Abort Fail",
            execution_mode="generated_protocol",
            method_ref=ref,
            created_at="2025-01-01T00:00:00",
        )
        # Reason: _start_job_run serializes against abort_requested and
        # short-circuits before calling start_run. The abort flag must be
        # raised after the run has started (inside _start_job_run's lock
        # region), so the polling loop sees it. The per-test monkeypatch
        # wrapper below injects the flag at that point.
        return job, spec, spec_hash, spec_hash

    def test_permanent_abort_failure_marks_failed_not_aborted(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-425 vm-agent abort error must mark the job failed, not
        aborted, even when the run later reports measured/completed."""
        job, _spec, _hash, _ = self._make_aborted_job(elabftw)

        # Permanent abort failure (502 from the instrument agent, not 425).
        vm_agent.abort_permanent_error = VmAgentError("instrument unreachable", status_code=502)
        # The run completes successfully on the instrument after the
        # abort attempt — the abort never actually stopped it.
        vm_agent.run_states = ["measured"]

        # Reason: _start_job_run serializes against abort_requested. Raise
        # the flag *after* the run is started (inside the locked region,
        # before _poll_run), so the polling loop triggers _try_abort.
        original_start = executor_wet._start_job_run

        def start_then_flag(job_: Job, protocol: str | int) -> str:
            run_id = original_start(job_, protocol)
            if run_id:
                job_.abort_requested = True
            return run_id

        monkeypatch.setattr(executor_wet, "_start_job_run", start_then_flag)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Abort failed" in (job.error or "")
        assert vm_agent.abort_calls == [job.run_id]
        # No writeback for a job the bridge could not control.
        assert elabftw.uploaded_files == []
        # The abort failure must not be recorded as a successful abort.
        assert not any(e["event"] == "execution_aborted" for e in job.events), job.events
        assert any(e["event"] == "abort_failed" for e in job.events)

    def test_permanent_abort_failure_does_not_override_already_failed_status(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once a permanent abort failure marks the job failed, an instrument
        ``aborted`` confirmation arriving later must not relabel it
        ``aborted`` (which would suppress writeback as if the abort
        succeeded)."""
        job, _spec, _hash, _ = self._make_aborted_job(elabftw)

        vm_agent.abort_permanent_error = VmAgentError("instrument unreachable", status_code=500)
        # Subsequent polls: the instrument later confirms it aborted.
        vm_agent.run_states = ["aborted"]

        # Reason: _start_job_run serializes against abort_requested. Raise
        # the flag after the run is started so _poll_run triggers _try_abort.
        original_start = executor_wet._start_job_run

        def start_then_flag(job_: Job, protocol: str | int) -> str:
            run_id = original_start(job_, protocol)
            if run_id:
                job_.abort_requested = True
            return run_id

        monkeypatch.setattr(executor_wet, "_start_job_run", start_then_flag)

        executor_wet(job)

        assert job.status == "failed"
        assert "Abort failed" in (job.error or "")
        assert not any(e["event"] == "execution_aborted" for e in job.events), job.events

    def test_425_too_early_still_retries_and_succeeds(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 425 'too early' abort response is not a permanent failure — the
        executor retries and (once the run is old enough) marks aborted when
        the run stops."""
        job, _spec, _hash, _ = self._make_aborted_job(elabftw)

        # First abort attempt is too early; second succeeds; run then
        # reports measured.
        vm_agent.abort_425_remaining = 1
        vm_agent.run_states = ["running", "measured"]

        # Reason: _start_job_run serializes against abort_requested. Raise
        # the flag after the run is started so _poll_run triggers _try_abort.
        original_start = executor_wet._start_job_run

        def start_then_flag(job_: Job, protocol: str | int) -> str:
            run_id = original_start(job_, protocol)
            if run_id:
                job_.abort_requested = True
            return run_id

        monkeypatch.setattr(executor_wet, "_start_job_run", start_then_flag)

        executor_wet(job)

        assert job.status == "aborted", job.events
        assert len(vm_agent.abort_calls) == 2
        assert elabftw.uploaded_files == []


class TestResultsContractForOperatorReview:
    """Regression tests for the MEDIUM state-contract issue
    (docs/abort-recovery.md). When the run reached a measured/completed
    state but the bridge cannot trust the output, the job must surface as
    ``unknown_requires_operator_review`` instead of ``completed``:

    - requested analysis raised — raw results still written back, but the
      job is not reported cleanly completed;
    - MDB/live/run fallbacks returned no well records at all — there is no
      explicit zero-well contract, so clean completion would be a lie.

    A valid zero-valued reading yields a well record with value ``0.0`` and
    stays ``completed`` (covered separately by
    ``test_zero_reading_is_preserved_in_results_html``): that case is
    distinct from "no wells returned".
    """

    @staticmethod
    def _make_method_spec() -> dict[str, Any]:
        return {
            "schema_name": "wallac.method",
            "schema_version": 1,
            "mode": "photometry",
            "name": "OD600",
            "plate_type": "96-well",
            "photometry": {
                "filter_id": "P610",
                "filter_name": "610nm",
                "read_time_seconds": 1.0,
            },
        }

    @staticmethod
    def _make_layout_spec() -> dict[str, Any]:
        return {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": [{"well_name": "A1", "role": "measured"}],
        }

    @staticmethod
    def _make_analysis_spec() -> dict[str, Any]:
        return {
            "schema_name": "wallac.analysis",
            "schema_version": 1,
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [],
            "exclusions": [],
            "outputs": ["raw_results", "analyzed_wells"],
        }

    def _make_full_job(self, elabftw: MockElabftwClient) -> Job:
        method_bytes, method_hash = canonicalize_and_hash(self._make_method_spec())
        layout_bytes, layout_hash = canonicalize_and_hash(self._make_layout_spec())
        analysis_bytes, analysis_hash = canonicalize_and_hash(self._make_analysis_spec())
        elabftw.add_upload(42, 5001, method_bytes)
        elabftw.add_upload(43, 5002, layout_bytes)
        elabftw.add_upload(44, 5003, analysis_bytes)
        return Job(
            job_id="test-job-contract",
            title="Contract Test",
            execution_mode="generated_protocol",
            method_ref={
                "object_id": 42,
                "hash": method_hash,
                "json_attachment_id": 5001,
            },
            layout_ref={
                "object_id": 43,
                "hash": layout_hash,
                "json_attachment_id": 5002,
            },
            analysis_ref={
                "object_id": 44,
                "hash": analysis_hash,
                "json_attachment_id": 5003,
            },
            created_at="2025-01-01T00:00:00",
        )

    def test_requested_analysis_failure_marks_operator_review(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A requested analysis pipeline that raises must surface as
        ``unknown_requires_operator_review`` and *still* persist the raw
        results, rather than being swallowed and reported ``completed``
        with raw-only output."""
        job = self._make_full_job(elabftw)

        # MDB returns one well with a real, non-zero reading so raw results
        # are non-empty and the analysis pipeline is genuinely attempted.
        vm_agent.job_wells = [{"well": "A01", "od": 0.123, "counts": 123456}]

        # The requested analysis pipeline fails outright (e.g. a malformed
        # threshold rule blows up inside AnalysisPipeline.run).
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("analysis pipeline exploded")

        monkeypatch.setattr(executor_wet.analysis, "run", _raise)

        executor_wet(job)

        assert job.status == "unknown_requires_operator_review", job.events
        assert any(e["event"] == "analysis_failed" for e in job.events), job.events
        assert any(e["event"] == "operator_review_required" for e in job.events), job.events
        # Raw results must still have been written back (preserve raw data).
        assert any(name.endswith("_raw_results.json") for name in elabftw.uploaded_files), (
            elabftw.uploaded_files
        )
        # The analyzed CSV must NOT have been uploaded — analysis failed.
        assert not any(name.endswith("_analyzed.csv") for name in elabftw.uploaded_files), (
            elabftw.uploaded_files
        )

    def test_no_wells_after_measured_run_marks_operator_review(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If MDB/live/run fallbacks yield no well records at all after the
        run reached a measured/completed state, the bridge must not report a
        clean completion. There is no explicit zero-well contract, so the
        job goes to ``unknown_requires_operator_review``."""
        job = self._make_full_job(elabftw)

        # Avoid the slow 10x3s MDB-assay retry loop — its retry logic is
        # orthogonal to this bug. We force "no new MDB assay" so the fallback
        # path runs immediately.
        monkeypatch.setattr(executor_wet, "_fetch_assay_wells", lambda _job: [])
        # Fallback then queries live_wells (empty after poll) and the run
        # endpoint (mock default returns no wells).
        assert vm_agent.live_batches == []

        executor_wet(job)

        assert job.status == "unknown_requires_operator_review", job.events
        assert any(e["event"] == "operator_review_required" for e in job.events), job.events
        # The fallback path was actually exercised (recorded its event).
        assert any(e["event"] == "assay_id_resolution_failed" for e in job.events), job.events
        # Nothing to write back when no wells were returned at all.
        assert elabftw.uploaded_files == [], elabftw.uploaded_files

    def test_zero_valued_reading_still_completes(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A well record with a zero reading (``0.0``) is a real measurement
        and must complete the job — distinct from "no wells returned".

        Generated_protocol mode without an analysis_ref keeps the analysis
        step unrequested, so the contract at the executor level pins here
        alongside ``test_zero_reading_is_preserved_in_results_html``.
        """
        method_spec = self._make_method_spec()
        method_bytes, method_hash = canonicalize_and_hash(method_spec)
        elabftw.add_upload(42, 5001, method_bytes)
        job = Job(
            job_id="test-job-zero",
            title="Zero Reading",
            execution_mode="generated_protocol",
            method_ref={
                "object_id": 42,
                "hash": method_hash,
                "json_attachment_id": 5001,
            },
            created_at="2025-01-01T00:00:00",
        )
        executor_wet.vm_agent.job_wells = [
            {"well": "A01", "primary_value": "", "od": 0.0, "counts": 999}
        ]

        executor_wet(job)

        assert job.status == "completed", job.events
        assert not any(e["event"] == "operator_review_required" for e in job.events), job.events
        assert any(name.endswith("_raw_results.json") for name in elabftw.uploaded_files), (
            elabftw.uploaded_files
        )

    def test_operator_review_job_never_emits_execution_completed(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for the MEDIUM state-contract issue: ``execution_completed``
        must not be emitted for a job that ends in
        ``unknown_requires_operator_review``. Writeback completion
        (``writeback_completed``) is a distinct, earlier signal and is
        permitted; only the terminal ``execution_completed`` event must be
        withheld until the promotion decision is final."""
        job = self._make_full_job(elabftw)
        vm_agent.job_wells = [{"well": "A01", "od": 0.123, "counts": 123456}]

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("analysis pipeline exploded")

        monkeypatch.setattr(executor_wet.analysis, "run", _raise)

        executor_wet(job)

        assert job.status == "unknown_requires_operator_review", job.events
        # Writeback completed signal IS allowed (raw results persisted)...
        assert any(e["event"] == "writeback_completed" for e in job.events), job.events
        # ...but the terminal completion event must NOT be emitted.
        assert not any(e["event"] == "execution_completed" for e in job.events), job.events
        assert any(e["event"] == "operator_review_required" for e in job.events)


class TestLayoutAcquisitionRoles:
    def test_excluded_wells_are_acquired_but_skipped_wells_are_not(self) -> None:
        job = Job(job_id="roles", title="Roles", execution_mode="generated_protocol")
        layout = {
            "wells": [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded"},
                {"well_name": "A3", "role": "skipped"},
            ]
        }

        wells, normalized = BridgeExecutor._measured_layout_wells(job, layout)
        results = BridgeExecutor._normalize_results(
            job,
            [
                {"well": "A01", "od": 1.0},
                {"well": "A02", "od": 2.0},
                {"well": "A03", "od": 3.0},
            ],
            layout,
        )

        assert wells == ["A1", "A2"]
        assert normalized == {"A1", "A2"}
        assert [result["well"] for result in results] == ["A1", "A2"]


# --- MEDIUM: existing_protocol protocol_id precedence for assay lookup ---


class TestExistingProtocolIdAssayLookup:
    """Regression for the MEDIUM protocol_id precedence issue.

    When ``existing_protocol`` mode resolves the protocol by ``protocol_id``
    (which takes precedence over ``protocol_name`` per
    ``_resolve_existing_protocol``), the post-run MDB assay lookup must NOT
    filter by the now-stale ``protocol_name``: the client-side label may no
    longer match the instrument's current rename. It must match MDB entries
    by authoritative protocol ID and reject unrelated concurrent assays.
    """

    def test_protocol_id_with_stale_name_still_fetches_results(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Instrument's installed protocol carries a renamed listing entry,
        # whose protocol_name differs from the stale client-side name on the
        # job. protocol_id is authoritative.
        renamed_name = "Absorbance 600nm (v2 1.0s)"
        stale_name = "Stale Old Absorbance @ 600 (1.0s)"

        class _AssayVmAgent(MockVmAgentClient):
            def get_jobs(self) -> list[dict[str, Any]]:  # type: ignore[override]
                return [
                    {
                        "assay_id": 200,
                        "protocol_name": renamed_name,
                        "protocol_id": 1001,
                    }
                ]

        agent = _AssayVmAgent()
        agent.add_protocol({"id": 1001, "name": renamed_name, "factory_preset": True})
        executor_wet.vm_agent = agent

        job = Job(
            job_id="test-existing-id-stale-name",
            title="Existing by ID",
            execution_mode="existing_protocol",
            protocol_id=1001,
            protocol_name=stale_name,
            created_at="2025-01-01T00:00:00",
        )

        # Keep max_assay_before=0 so _find_assay_after's jid>0 match returns
        # immediately; the snapshot-then-new-assay dynamic is orthogonal to
        # the name-filter bug under test.
        monkeypatch.setattr(executor_wet, "_snapshot_max_assay_id", lambda _job: True)

        executor_wet(job)

        assert job.status == "completed", job.events
        # The MDB assay was resolved despite the protocol_name mismatch,
        # proving the stale-name filter was not applied.
        assert any(e["event"] == "assay_id_resolved" for e in job.events), job.events
        assert job.assay_prot_id == 200
        assert any(name.endswith("_raw_results.json") for name in elabftw.uploaded_files), (
            elabftw.uploaded_files
        )

    def test_protocol_name_still_filters_when_no_id(self) -> None:
        """Unit guard: when protocol_id is NOT set, the name filter is still
        passed through to _find_assay_after (the precedence is one-way)."""
        from bridge.executor import _find_assay_after

        class _Agent:
            def get_jobs(self) -> list[dict[str, Any]]:
                return [
                    {"assay_id": 5, "protocol_name": "Match Me"},
                    {"assay_id": 7, "protocol_name": "Other"},
                ]

        assert _find_assay_after(_Agent(), max_before=0, proto_name="Match Me") == 5
        assert _find_assay_after(_Agent(), max_before=0, proto_name="") == 7

    def test_protocol_id_rejects_unrelated_newer_assay(self) -> None:
        from bridge.executor import _find_assay_after

        class _Agent:
            def get_jobs(self) -> list[dict[str, Any]]:
                return [
                    {
                        "assay_id": 5,
                        "protocol_name": "Renamed protocol",
                        "protocol_id": 1001,
                    },
                    {
                        "assay_id": 7,
                        "protocol_name": "Concurrent run",
                        "protocol_id": 9999,
                    },
                ]

        assert (
            _find_assay_after(
                _Agent(),
                max_before=0,
                proto_name="Stale client name",
                proto_id=1001,
            )
            == 5
        )
