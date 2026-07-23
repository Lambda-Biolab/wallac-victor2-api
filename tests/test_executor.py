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
        self.abort_responses: list[dict[str, Any]] | None = None
        self.abort_default_response: dict[str, Any] = {
            "ok": True,
            "is_running": False,
            "state_text": "aborted",
        }
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

    def abort_run(self, run_id: str) -> dict[str, Any]:
        self.abort_calls.append(run_id)
        if self.abort_permanent_error is not None:
            raise self.abort_permanent_error
        if self.abort_425_remaining:
            self.abort_425_remaining -= 1
            raise VmAgentError("too early", status_code=425)
        if self.abort_responses is not None:
            return self.abort_responses.pop(0)
        return dict(self.abort_default_response)


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

    def _stage_full_refs(
        self,
        elabftw: MockElabftwClient,
        method_spec: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Stage method/layout/analysis uploads and return (method_ref,
        layout_ref, analysis_ref) for the ``generated_protocol`` schema
        contract that now requires all three signed refs."""
        if method_spec is None:
            method_spec, _, _ = self._make_method_spec()
        layout_spec = self._make_layout_spec()
        analysis_spec = self._make_analysis_spec()
        method_bytes, method_hash = canonicalize_and_hash(method_spec)
        layout_bytes, layout_hash = canonicalize_and_hash(layout_spec)
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis_spec)
        elabftw.add_upload(42, 5001, method_bytes)
        elabftw.add_upload(43, 5002, layout_bytes)
        elabftw.add_upload(44, 5003, analysis_bytes)
        return (
            {"object_id": 42, "hash": method_hash, "json_attachment_id": 5001},
            {"object_id": 43, "hash": layout_hash, "json_attachment_id": 5002},
            {"object_id": 44, "hash": analysis_hash, "json_attachment_id": 5003},
        )

    def _make_job(
        self,
        method_ref: dict[str, Any],
        dry_run: bool = True,
        layout_ref: dict[str, Any] | None = None,
        analysis_ref: dict[str, Any] | None = None,
    ) -> Job:
        # Reason: generated_protocol contract requires all three signed refs
        # (schemas.py ExecutionMode docstring + docs/plans/
        # wallac-protocol-authoring.md). Tests covering only method_ref
        # integrity pass empty layout/analysis refs explicitly to assert
        # the strict-missing-ref handling; full-path tests pass real refs.
        return Job(
            job_id="test-job-001",
            title="Test Job",
            execution_mode="generated_protocol",
            method_ref=method_ref,
            layout_ref=layout_ref or {},
            analysis_ref=analysis_ref or {},
            created_at="2025-01-01T00:00:00",
        )

    def _execute_method_spec(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        spec: dict[str, Any],
    ) -> Job:
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw, spec)
        job = self._make_job(
            method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
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
        elabftw: MockElabftwClient,
        ref: dict[str, Any],
        error_fragment: str,
    ) -> None:
        # Reason: a malformed method_ref must fail closed even when layout/
        # analysis refs are present and valid. Stage a full valid set and
        # override only the (malformed) method_ref so the test stays pinned
        # to method_ref integrity rather than the strict missing-ref gate.
        _method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        job = self._make_job(ref, dry_run=False, layout_ref=layout_ref, analysis_ref=analysis_ref)

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
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        job = self._make_job(
            method_ref, dry_run=False, layout_ref=layout_ref, analysis_ref=analysis_ref
        )

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
        method_spec, _, _ = self._make_method_spec()
        method_bytes, method_hash = canonicalize_and_hash(method_spec)
        elabftw.add_upload(42, 5001, method_bytes)
        # Stage the remaining signed layout/analysis refs alongside the
        # legacy-named method_ref to satisfy the strict generated_protocol
        # contract.
        _m, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        legacy_method_ref = {
            "object_id": 42,
            "hash": method_hash,
            "attachment_id": 5001,
        }
        job = self._make_job(
            legacy_method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
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
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        vm_agent.fail_get_jobs = True
        job = self._make_job(
            method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
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
        # Stage valid layout/analysis so the failure is pinned to method download.
        _m, layout_ref, analysis_ref = self._stage_full_refs(elabftw)

        # Use a wrong hash
        wrong_hash = "a" * 64

        ref = {
            "object_id": 42,
            "hash": wrong_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(
            ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
        )

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
        _m, layout_ref, analysis_ref = self._stage_full_refs(elabftw)

        # Ref without hash (but with valid object_id and attachment_id)
        ref = {
            "object_id": 42,
            "json_attachment_id": 5001,
        }
        job = self._make_job(
            ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
        )

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
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        job = self._make_job(
            method_ref, dry_run=True, layout_ref=layout_ref, analysis_ref=analysis_ref
        )

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
        _m, layout_ref, analysis_ref = self._stage_full_refs(elabftw)

        wrong_hash = "b" * 64
        ref = {
            "object_id": 42,
            "hash": wrong_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(ref, dry_run=True, layout_ref=layout_ref, analysis_ref=analysis_ref)

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

    def test_clone_failure_fails_closed_no_factory_fallback(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """Clone failure must NOT fall back to the factory protocol.

        The generated-protocol run needs a per-plate clone so the MDB
        PlateMap covers exactly the signed layout's measured/excluded wells.
        Running against the factory preset instead would acquire the
        wrong wells from live hardware. The job must fail closed from a
        single point (:meth:`_execute_generated_protocol`), and no
        physical run may be started — the no-physical-work guarantee
        (docs/architecture-direct-submit.md validated workflow) applied.
        """
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        vm_agent.fail_clone = True
        job = self._make_job(
            method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
        )

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Protocol clone or plate-map apply failed" in (job.error or "")
        assert any(event["event"] == "protocol_clone_failed" for event in job.events), job.events
        # No physical run was started against the factory protocol.
        assert vm_agent._runs == {}, vm_agent._runs
        assert vm_agent.requested_job_ids == [], vm_agent.requested_job_ids
        # Clone failed before any partial copy existed on the instrument,
        # so there is nothing to clean up.
        assert vm_agent.deleted_protocols == [], vm_agent.deleted_protocols

    def test_plate_map_failure_fails_closed_and_cleans_partial_clone(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """If clone succeeds but plate-map apply fails, the partial clone
        must be cleaned up and the job failed — never run against the
        factory preset.

        Cloning creates a stub protocol on the instrument MDB; without
        cleanup the orphan would shadow the factory preset on the next
        assay lookup. The no-physical-work guarantee extends to clone
        side-effects, not just run start.
        """
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        # Capture the clone id the executor will mint so we can assert
        # the partial clone was deleted exactly once.
        vm_agent.fail_plate_map = True
        job = self._make_job(
            method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
        )

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Protocol clone or plate-map apply failed" in (job.error or "")
        assert any(event["event"] == "plate_map_apply_failed" for event in job.events), job.events
        # No physical run was started.
        assert vm_agent._runs == {}, vm_agent._runs
        assert vm_agent.requested_job_ids == [], vm_agent.requested_job_ids
        # The partial clone created by clone_protocol was best-effort
        # cleaned up by the executor before failing the job.
        assert len(vm_agent.deleted_protocols) == 1, vm_agent.deleted_protocols

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
        method_ref, layout_ref, analysis_ref = self._stage_full_refs(elabftw)
        job = self._make_job(
            method_ref,
            dry_run=False,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
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


class TestGeneratedProtocolStrictRefs:
    """Regression tests for the PREPR blocker: ``generated_protocol``
    execution must require ``method_ref``, ``layout_ref``, *and*
    ``analysis_ref`` before dry-run success or any wet hardware start.
    The ``generated_protocol`` schema contract (``ExecutionMode`` docstring
    in schemas.py + docs/plans/wallac-protocol-authoring.md "Validated
    workflow") requires signed method.json, layout.json, and analysis.json;
    an absent ref means the submission cannot be interpreted and must fail
    closed *before* downloads/hash-checks/validation/dry-run/run start.

    Previous behavior short-circuited absent layout/analysis refs to an
    empty dict, passed them through the schema validator (which silently
    skipped empty structs), and proceeded to ``dry_run_complete`` or a
    hardware run — violating the no-physical-work guarantee for an
    incomplete submission. These tests pin the deterministic failure.
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

    def _stage_full(
        self,
        elabftw: MockElabftwClient,
        *,
        skip_method: bool = False,
        skip_layout: bool = False,
        skip_analysis: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Stage every signed spec upload; return ``(method_ref, layout_ref,
        analysis_ref)``. Skipped refs come back as ``{}`` to mirror the
        shipped-but-incomplete submission that would have the strict gate."""
        method = self._make_method_spec()
        layout = {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": [{"well_name": "A1", "role": "measured"}],
        }
        analysis = {
            "schema_name": "wallac.analysis",
            "schema_version": 1,
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [],
            "exclusions": [],
            "outputs": ["raw_results", "analyzed_wells"],
        }
        method_ref: dict[str, Any] = {}
        layout_ref: dict[str, Any] = {}
        analysis_ref: dict[str, Any] = {}

        if not skip_method:
            mb, mh = canonicalize_and_hash(method)
            elabftw.add_upload(42, 5001, mb)
            method_ref = {"object_id": 42, "hash": mh, "json_attachment_id": 5001}
        if not skip_layout:
            lb, lh = canonicalize_and_hash(layout)
            elabftw.add_upload(43, 5002, lb)
            layout_ref = {"object_id": 43, "hash": lh, "json_attachment_id": 5002}
        if not skip_analysis:
            ab, ah = canonicalize_and_hash(analysis)
            elabftw.add_upload(44, 5003, ab)
            analysis_ref = {"object_id": 44, "hash": ah, "json_attachment_id": 5003}
        return method_ref, layout_ref, analysis_ref

    def _make_job(
        self,
        method_ref: dict[str, Any],
        layout_ref: dict[str, Any],
        analysis_ref: dict[str, Any],
    ) -> Job:
        return Job(
            job_id="test-strict-refs",
            title="Strict Refs",
            execution_mode="generated_protocol",
            method_ref=method_ref,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
            created_at="2025-01-01T00:00:00",
        )

    @staticmethod
    def _assert_missing_ref_blocks(job: Job, vm_agent: MockVmAgentClient) -> None:
        """The strict-missing-ref gate fires *before* any download attempt,
        validation, dry-run success, or instrument work."""
        # Reason: pytest rewrites `assert (... , ...)` as a tuple literal,
        # which is always truthy. Split into a dedicated assertion so the
        # behavioral gate is actually exercised.
        assert any(event["event"] == "missing_required_ref" for event in job.events), job.events
        assert not any(e["event"] == "specs_downloaded" for e in job.events), job.events
        assert not any(e["event"] == "specs_validated" for e in job.events), job.events
        assert not any(e["event"] == "dry_run_complete" for e in job.events), job.events
        # No instrument work, ever, for a missing ref.
        assert vm_agent.requested_job_ids == [], vm_agent.requested_job_ids
        assert vm_agent.deleted_protocols == [], vm_agent.deleted_protocols
        assert vm_agent.deleted_protocols == [], vm_agent.deleted_protocols

    @pytest.mark.parametrize(
        ("skip_method", "skip_layout", "skip_analysis", "expected_missing"),
        [
            (True, False, False, "method_ref"),
            (False, True, False, "layout_ref"),
            (False, False, True, "analysis_ref"),
        ],
    )
    def test_missing_ref_blocks_dry_run(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        skip_method: bool,
        skip_layout: bool,
        skip_analysis: bool,
        expected_missing: str,
    ) -> None:
        """A missing ref must fail the job deterministically before dry-run
        success. The failure names the missing ref and never downloads,
        validates, or reaches ``dry_run_complete``."""
        method_ref, layout_ref, analysis_ref = self._stage_full(
            elabftw,
            skip_method=skip_method,
            skip_layout=skip_layout,
            skip_analysis=skip_analysis,
        )
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor(job)

        assert job.status == "failed", job.events
        assert "Missing required ref(s) for generated_protocol mode" in (job.error or "")
        assert expected_missing in job.error, job.error
        self._assert_missing_ref_blocks(job, vm_agent)

    @pytest.mark.parametrize(
        ("skip_layout", "skip_analysis", "expected_missing"),
        [
            (True, False, "layout_ref"),
            (False, True, "analysis_ref"),
        ],
    )
    def test_missing_ref_blocks_wet_hardware_start(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        skip_layout: bool,
        skip_analysis: bool,
        expected_missing: str,
    ) -> None:
        """A missing ref must fail the job deterministically before any wet
        hardware start — the same gate as dry-run, never bypassed by
        ``dry_run=False``."""
        method_ref, layout_ref, analysis_ref = self._stage_full(
            elabftw,
            skip_layout=skip_layout,
            skip_analysis=skip_analysis,
        )
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Missing required ref(s) for generated_protocol mode" in (job.error or "")
        assert expected_missing in job.error, job.error
        self._assert_missing_ref_blocks(job, vm_agent)
        # No run on the instrument, ever.
        assert vm_agent._runs == {}, vm_agent._runs

    def test_all_refs_present_passes_strict_gate_in_dry_run(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Sanity: a complete ref set must still proceed beyond the strict
        gate to download, validate, and reach ``dry_run_complete`` —
        confirming the guard only fires on actually-missing refs."""
        method_ref, layout_ref, analysis_ref = self._stage_full(elabftw)
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor(job)

        assert job.status == "completed", job.events
        assert not any(e["event"] == "missing_required_ref" for e in job.events), job.events
        assert any(e["event"] == "specs_downloaded" for e in job.events), job.events
        assert any(e["event"] == "specs_validated" for e in job.events), job.events
        assert any(e["event"] == "dry_run_complete" for e in job.events), job.events


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
        # Reason: generated_protocol contract requires all three signed refs
        # (schemas.py ExecutionMode docstring). Stage valid layout/analysis
        # uploads so the strict-missing-ref gate does not short-circuit the
        # abort-failure path under test.
        layout = {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": [{"well_name": "A1", "role": "measured"}],
        }
        layout_bytes, layout_hash = canonicalize_and_hash(layout)
        elabftw.add_upload(43, 5002, layout_bytes)
        analysis = {
            "schema_name": "wallac.analysis",
            "schema_version": 1,
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [],
            "exclusions": [],
            "outputs": ["raw_results", "analyzed_wells"],
        }
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis)
        elabftw.add_upload(44, 5003, analysis_bytes)
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

    def test_abort_response_ok_false_keeps_polling_for_measured(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An HTTP-200 abort response with ``ok=false`` and ``is_running=true``
        means the vm-agent accepted the request but the instrument is still
        running. The bridge must keep polling and let a subsequent measured
        run complete with results — not mislabel the run aborted or skip
        writeback (regression for the 200-ok=false vm-agent contract)."""
        job, _spec, _hash, _ = self._make_aborted_job(elabftw)

        vm_agent.abort_responses = [
            {"ok": False, "is_running": True, "state_text": "still running"},
        ]
        vm_agent.run_states = ["measured"]

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

        assert job.status == "completed", job.events
        assert vm_agent.abort_calls == [job.run_id]
        assert any(e["event"] == "abort_in_progress" for e in job.events), job.events
        assert any(name.endswith("_raw_results.json") for name in elabftw.uploaded_files)


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

        Uses the full ``_make_full_job`` helper so the strict-missing-ref
        gate does not short-circuit. The operator-review contract at the
        executor level pins here alongside
        ``test_zero_reading_is_preserved_in_results_html``.
        """
        job = self._make_full_job(elabftw)
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


class TestGeneratedProtocolSpecValidation:
    """Regression tests for the PREPR blocker: the executor downloaded
    hash-valid method/layout/analysis dicts but did not fail them closed
    through MethodSpec/LayoutSpec/AnalysisSpec schema/version validation
    before dry-run success or hardware start (docs/plans/
    wallac-protocol-authoring.md "Validated workflow").

    The download path only verifies the SHA-256 of the attachment bytes. It
    does not check that the parsed JSON conforms to a supported schema
    version, has valid well roles, or uses in-range well names. An
    unsupported schema version or a malformed layout must fail the job
    *before* any instrument work or dry-run success, surface the
    ``spec_validation_failed`` event, and preserve the canonical dict (no
    clone, no run, no protocol delete) so a retry can re-submit a corrected
    signed object.
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
    def _make_layout_spec(
        wells: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if wells is None:
            wells = [{"well_name": "A1", "role": "measured"}]
        return {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": wells,
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

    def _stage_specs(
        self,
        elabftw: MockElabftwClient,
        method: dict[str, Any],
        layout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stage method + layout + analysis uploads and return refs dict.

        Reason: the ``generated_protocol`` schema contract requires all
        three signed refs before dry-run success or any hardware start — a
        missing ref now fails the job upstream in :meth:`_load_generated_specs`
        and never reaches this validator's failure mode.
        Default-staging a valid layout and a valid analysis keeps every spec-
        validation test pinned to the schema/version/role parsing path it
        claims to exercise. Tests that need an invalid layout override the
        ``layout`` kwarg; tests that need an invalid analysis override the
        analysis_ref entry via :meth:`_stage_analysis` (which re-stages at
        the same upload slot, shadowing this default).
        """
        method_bytes, method_hash = canonicalize_and_hash(method)
        elabftw.add_upload(42, 5001, method_bytes)
        if layout is None:
            layout = self._make_layout_spec()
        layout_bytes, layout_hash = canonicalize_and_hash(layout)
        elabftw.add_upload(43, 5002, layout_bytes)
        analysis = self._make_analysis_spec()
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis)
        elabftw.add_upload(44, 5003, analysis_bytes)
        refs: dict[str, Any] = {
            "method_ref": {
                "object_id": 42,
                "hash": method_hash,
                "json_attachment_id": 5001,
            },
            "layout_ref": {
                "object_id": 43,
                "hash": layout_hash,
                "json_attachment_id": 5002,
            },
            "analysis_ref": {
                "object_id": 44,
                "hash": analysis_hash,
                "json_attachment_id": 5003,
            },
        }
        return refs

    def _make_job(
        self,
        refs: dict[str, Any],
    ) -> Job:
        return Job(
            job_id="test-job-spec-validation",
            title="Spec Validation",
            execution_mode="generated_protocol",
            method_ref=refs["method_ref"],
            layout_ref=refs.get("layout_ref", {}),
            analysis_ref=refs.get("analysis_ref", {}),
            created_at="2025-01-01T00:00:00",
        )

    def _assert_no_physical_work(
        self,
        job: Job,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """The failed validation must not start a run, clone a protocol,
        or request any MDB results."""
        assert job.run_id == ""
        assert vm_agent._runs == {}, vm_agent._runs
        assert vm_agent.deleted_protocols == [], vm_agent.deleted_protocols
        assert vm_agent.requested_job_ids == [], vm_agent.requested_job_ids

    def test_unsupported_method_schema_version_fails_in_dry_run(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A method spec with an unsupported schema version (e.g. v2) must
        fail closed in the dry-run path before ``dry_run_complete`` is
        emitted. The hash is valid; only the schema version is not."""
        method = self._make_method_spec()
        method["schema_version"] = 2

        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        assert not any(e["event"] == "dry_run_complete" for e in job.events), job.events
        assert not any(e["event"] == "specs_validated" for e in job.events), job.events

    def test_unsupported_method_schema_version_fails_in_wet_path(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """In the wet path, an unsupported method schema version must fail
        before any instrument work is started, even when a matching
        protocol is available on the vm-agent."""
        method = self._make_method_spec()
        method["schema_version"] = 2

        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events)
        self._assert_no_physical_work(job, vm_agent)

    def test_invalid_layout_role_fails_in_dry_run(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A layout spec with an unknown ``role`` must fail closed in the
        dry-run path. ``WellSpec.from_dict`` raises ``ValueError`` for
        invalid roles; the executor must convert that into a fail-closed
        job rather than silently dropping the well."""
        method = self._make_method_spec()
        layout = self._make_layout_spec([{"well_name": "A1", "role": "totally_bogus"}])

        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert "Invalid well role" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events)
        assert not any(e["event"] == "dry_run_complete" for e in job.events)

    def test_invalid_layout_role_fails_in_wet_path_before_run_start(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """In the wet path, an invalid layout role must fail closed before
        ``_match_protocol_from_method`` runs or any clone/start is issued,
        even when a matching factory protocol exists on the instrument."""
        method = self._make_method_spec()
        layout = self._make_layout_spec([{"well_name": "A1", "role": "magic"}])

        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert "Invalid well role" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events)
        # No protocol matching/clone/run since validation precedes them.
        assert not any(e["event"] == "protocol_matched" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_invalid_well_name_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """An out-of-range well name must fail closed before any instrument
        work — the layout cannot be applied to the instrument's plate map."""
        method = self._make_method_spec()
        layout = self._make_layout_spec([{"well_name": "Z9", "role": "measured"}])

        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert "Invalid well name" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events)
        self._assert_no_physical_work(job, vm_agent)

    def test_unsupported_analysis_schema_version_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """An analysis spec with an unsupported schema version must fail
        closed before any instrument work, even though analysis is only
        applied at write-back time. The signed version could not be
        interpreted, so the run must not start."""
        method = self._make_method_spec()
        analysis = {
            "schema_name": "wallac.analysis",
            "schema_version": 2,  # unsupported
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [],
            "exclusions": [],
            "outputs": [],
        }
        refs = self._stage_specs(elabftw, method)
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis)
        elabftw.add_upload(44, 5003, analysis_bytes)
        refs["analysis_ref"] = {
            "object_id": 44,
            "hash": analysis_hash,
            "json_attachment_id": 5003,
        }
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for analysis spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events)
        self._assert_no_physical_work(job, vm_agent)

    def test_valid_specs_emit_validated_event_before_dry_run_complete(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A complete, schema-valid spec set must emit ``specs_validated``
        before ``dry_run_complete`` — proving the gate is exercised on the
        success path too, not just the failure path."""
        method = self._make_method_spec()
        layout = self._make_layout_spec()
        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor(job)

        assert job.status == "completed", job.events
        validated_idx = next(
            (i for i, e in enumerate(job.events) if e["event"] == "specs_validated"),
            None,
        )
        dry_run_idx = next(
            (i for i, e in enumerate(job.events) if e["event"] == "dry_run_complete"),
            None,
        )
        assert validated_idx is not None, job.events
        assert dry_run_idx is not None, job.events
        assert validated_idx < dry_run_idx, job.events

    def _stage_analysis(
        self,
        elabftw: MockElabftwClient,
        refs: dict[str, Any],
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage an analysis spec alongside refs produced by _stage_specs."""
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis)
        elabftw.add_upload(44, 5003, analysis_bytes)
        refs["analysis_ref"] = {
            "object_id": 44,
            "hash": analysis_hash,
            "json_attachment_id": 5003,
        }
        return refs

    def test_method_missing_required_mode_field_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A hash-valid method spec missing the required ``mode`` field
        surfaces a ``KeyError`` from ``MethodSpec.from_dict`` direct-key
        access (``d["mode"]``). The executor must normalize that into the
        existing fail-closed path (``spec_validation_failed`` + ``failed``)
        rather than letting it propagate out of the worker thread.
        """
        method = self._make_method_spec()
        del method["mode"]
        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_method_missing_required_name_field_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A hash-valid method spec missing the required ``name`` field
        surfaces a ``KeyError`` from ``d["name"]`` and must fail closed
        before dry-run success or any hardware start, identically to the
        schema-version guard.
        """
        method = self._make_method_spec()
        del method["name"]
        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_method_missing_photometry_settings_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A photometry method spec without the ``photometry`` block
        surfaces a ``KeyError`` (the parser uses ``d["photometry"]`` rather
        than ``.get``), which the executor must route to the fail-closed
        path instead of crashing the worker thread.
        """
        method = self._make_method_spec()
        del method["photometry"]
        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_method_wrong_typed_read_time_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A hash-valid method whose ``photometry.read_time_seconds`` is a
        list (not a number) raises ``TypeError`` on ``float(...)`` coercion
        inside ``PhotometrySettings.from_dict``. The executor must normalize
        that into the fail-closed path rather than letting it propagate.
        """
        method = self._make_method_spec()
        method["photometry"]["read_time_seconds"] = []
        refs = self._stage_specs(elabftw, method)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_layout_missing_wells_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A layout spec missing the required ``wells`` field surfaces a
        ``KeyError`` from ``LayoutSpec.from_dict`` (``d["wells"]``) and
        must fail closed before dry-run or run start.
        """
        method = self._make_method_spec()
        layout = {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
        }  # no 'wells'
        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_layout_well_missing_required_name_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A layout well dict missing ``well_name`` surfaces a ``KeyError``
        from ``WellSpec.from_dict`` (``d["well_name"]``) and must fail
        closed rather than silently dropping the well.
        """
        method = self._make_method_spec()
        layout = self._make_layout_spec([{"role": "measured"}])  # no 'well_name'
        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_layout_wells_wrong_type_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """A layout whose ``wells`` is a string (not a list of dicts) raises
        ``TypeError`` when ``WellSpec.from_dict`` indexes into each entry.
        The executor must route this malformed-but-hash-valid layout into the
        fail-closed path instead of crashing the worker.
        """
        method = self._make_method_spec()
        layout = self._make_layout_spec()
        layout["wells"] = "not-a-list"
        refs = self._stage_specs(elabftw, method, layout)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for layout spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_threshold_missing_required_value_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """An analysis threshold rule missing the required ``value`` field
        surfaces a ``KeyError`` from ``ThresholdRule.from_dict`` and must
        fail closed before any instrument work, even though analysis is
        only applied at write-back time.
        """
        method = self._make_method_spec()
        analysis = {
            "schema_name": "wallac.analysis",
            "schema_version": 1,
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [
                {"name": "t", "metric": "primary_value", "operator": ">="},  # no 'value'
            ],
            "exclusions": [],
            "outputs": [],
        }
        refs = self._stage_specs(elabftw, method)
        refs = self._stage_analysis(elabftw, refs, analysis)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for analysis spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    def test_threshold_wrong_typed_value_fails_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """An analysis threshold whose ``value`` is a dict (not a number)
        raises ``TypeError`` on ``float(...)`` coercion. The executor must
        normalize that into the fail-closed path before run start.
        """
        method = self._make_method_spec()
        analysis = {
            "schema_name": "wallac.analysis",
            "schema_version": 1,
            "blank_subtraction": {"enabled": False, "blank_wells": []},
            "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
            "normalization": {"enabled": False, "control_type": "", "target_value": 1.0},
            "thresholds": [
                {"name": "t", "metric": "primary_value", "operator": ">=", "value": {}},
            ],
            "exclusions": [],
            "outputs": [],
        }
        refs = self._stage_specs(elabftw, method)
        refs = self._stage_analysis(elabftw, refs, analysis)
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for analysis spec" in job.error
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    @pytest.mark.parametrize(
        ("kind", "make_invalid_spec", "expected_message_fragment"),
        [
            (
                "method",
                lambda: {},
                "Spec validation failed for method spec",
            ),
            (
                "layout",
                lambda: {},
                "Spec validation failed for layout spec",
            ),
            (
                "analysis",
                lambda: {},
                "Spec validation failed for analysis spec",
            ),
        ],
    )
    def test_empty_hash_valid_spec_dict_fails_closed(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        kind: str,
        make_invalid_spec,
        expected_message_fragment: str,
    ) -> None:
        """A hash-valid attachment that decodes to an empty JSON object
        ``{}`` must fail closed through the schema validator before
        ``dry_run_complete`` — removing the previous defensive skip that
        silently treated an empty spec as valid.

        The empty dict feeds ``validate_schema_identity("", 0)`` which
        raises ``BridgeError(SCHEMA_UNSUPPORTED)``; the executor must
        route that into the existing ``spec_validation_failed`` event +
        ``failed`` status, never reaching dry-run success.
        """
        method = self._make_method_spec()
        refs = self._stage_specs(elabftw, method)
        empty_spec = make_invalid_spec()
        empty_bytes, empty_hash = canonicalize_and_hash(empty_spec)
        slot = {"method": 42, "layout": 43, "analysis": 44}[kind]
        upload = {"method": 5001, "layout": 5002, "analysis": 5003}[kind]
        elabftw.add_upload(slot, upload, empty_bytes)
        refs[f"{kind}_ref"] = {
            "object_id": slot,
            "hash": empty_hash,
            "json_attachment_id": upload,
        }
        job = self._make_job(refs)

        executor(job)

        assert job.status == "failed", job.events
        assert expected_message_fragment in (job.error or "")
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        # Empty spec must never bypass validation into a no-op dry run.
        assert not any(e["event"] == "dry_run_complete" for e in job.events), job.events
        assert not any(e["event"] == "specs_validated" for e in job.events), job.events

    def test_empty_method_spec_dict_fails_closed_in_wet_path(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """In the wet path, an empty (hash-valid) method spec must fail
        before any protocol matching, cloning, or run start."""
        method = self._make_method_spec()
        refs = self._stage_specs(elabftw, method)
        empty_bytes, empty_hash = canonicalize_and_hash({})
        elabftw.add_upload(42, 5001, empty_bytes)
        refs["method_ref"] = {
            "object_id": 42,
            "hash": empty_hash,
            "json_attachment_id": 5001,
        }
        job = self._make_job(refs)

        executor_wet(job)

        assert job.status == "failed", job.events
        assert "Spec validation failed for method spec" in (job.error or "")
        assert any(e["event"] == "spec_validation_failed" for e in job.events), job.events
        self._assert_no_physical_work(job, vm_agent)

    @pytest.mark.parametrize(
        "slot,upload,kind,expected",
        [
            (45, 5001, "method", "wallac.method"),
            (45, 5002, "layout", "wallac.layout"),
            (45, 5003, "analysis", "wallac.analysis"),
        ],
    )
    def test_wrong_kind_ref_fails_closed(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        slot: int,
        upload: int,
        kind: str,
        expected: str,
    ) -> None:
        """A hash-valid ref pointing at the wrong schema kind must fail
        closed before dry-run/hardware, even though ``validate_schema_identity``
        would otherwise accept the schema globally.

        The executor must check the expected ``schema_name`` for the ref
        slot *before* running the parser, so a ``method_ref`` whose bytes
        describe a ``wallac.layout`` object (or vice versa) cannot silently
        flow into the matching/layout/analysis path.

        For each slot, the test overrides the ref with bytes encoding the
        *opposite* kind of spec. The method slot receives a layout-shaped
        spec, the layout slot receives an analysis-shaped spec, and the
        analysis slot receives a method-shaped spec — three concrete
        permutations of the same wrong-kind class of bug.
        """
        method = self._make_method_spec()
        refs = self._stage_specs(elabftw, method)
        opposite = {
            "method": self._make_layout_spec,
            "layout": self._make_analysis_spec,
            "analysis": self._make_method_spec,
        }[kind]
        wrong_bytes, wrong_hash = canonicalize_and_hash(opposite())
        elabftw.add_upload(slot, upload, wrong_bytes)
        refs[f"{kind}_ref"] = {
            "object_id": slot,
            "hash": wrong_hash,
            "json_attachment_id": upload,
        }
        job = self._make_job(refs)

        executor(job)

        assert job.status == "failed", job.events
        assert "expected schema_name" in (job.error or ""), job.error
        assert any(
            f"expected={expected}" in e["detail"] and e["event"] == "spec_validation_failed"
            for e in job.events
        ), job.events
        self._assert_no_physical_work(job, vm_agent)


class TestGeneratedProtocolZeroAcquisitionLayout:
    """Regression tests for the PREPR blocker: a generated layout whose
    wells are empty (``wells: []``) or entirely ``skipped`` produces a
    zero-acquisition MDB PlateMap. The executor must fail the job
    deterministically *before* ``dry_run_complete``, protocol matching/
    clone, or any hardware start — never report success for a no-op run,
    and never reach :meth:`_clone_for_layout` with an empty well set (which
    previously fell back to the factory protocol and acquired all 96 wells).

    These tests reuse the existing ``spec_validation_failed`` / ``failed`` /
    ``execution_failed`` failure vocabulary plus the dedicated
    ``layout_no_acquired_wells`` event so consumers can distinguish a zero-
    acquisition layout failure from a syntax-level spec failure.
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

    @staticmethod
    def _make_layout_spec(wells: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_name": "wallac.layout",
            "schema_version": 1,
            "plate_type": "96-well",
            "wells": wells,
        }

    def _stage_full(
        self,
        elabftw: MockElabftwClient,
        layout_wells: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        method = self._make_method_spec()
        layout = self._make_layout_spec(layout_wells)
        analysis = self._make_analysis_spec()
        method_bytes, method_hash = canonicalize_and_hash(method)
        layout_bytes, layout_hash = canonicalize_and_hash(layout)
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis)
        elabftw.add_upload(42, 5001, method_bytes)
        elabftw.add_upload(43, 5002, layout_bytes)
        elabftw.add_upload(44, 5003, analysis_bytes)
        return (
            {"object_id": 42, "hash": method_hash, "json_attachment_id": 5001},
            {"object_id": 43, "hash": layout_hash, "json_attachment_id": 5002},
            {"object_id": 44, "hash": analysis_hash, "json_attachment_id": 5003},
        )

    def _make_job(
        self,
        method_ref: dict[str, Any],
        layout_ref: dict[str, Any],
        analysis_ref: dict[str, Any],
    ) -> Job:
        return Job(
            job_id="test-zero-acq",
            title="Zero Acq",
            execution_mode="generated_protocol",
            method_ref=method_ref,
            layout_ref=layout_ref,
            analysis_ref=analysis_ref,
            created_at="2025-01-01T00:00:00",
        )

    @staticmethod
    def _assert_no_physical_work_or_dry_run(
        job: Job,
        vm_agent: MockVmAgentClient,
    ) -> None:
        # Reason: the zero-acquisition gate fires before dry-run success
        # and any hardware work, so neither signal may have surfaced.
        assert job.status == "failed", job.events
        assert any(e["event"] == "layout_no_acquired_wells" for e in job.events), job.events
        assert any(e["event"] == "execution_failed" for e in job.events), job.events
        assert not any(e["event"] == "dry_run_complete" for e in job.events), job.events
        assert not any(e["event"] == "protocol_matched" for e in job.events), job.events
        assert job.run_id == ""
        assert vm_agent._runs == {}, vm_agent._runs
        assert vm_agent.deleted_protocols == [], vm_agent.deleted_protocols
        assert vm_agent.requested_job_ids == [], vm_agent.requested_job_ids

    @pytest.mark.parametrize(
        ("layout_wells", "label"),
        [
            ([], "empty wells list"),
            ([{"well_name": "A1", "role": "skipped"}], "single skipped well"),
            (
                [
                    {"well_name": "A1", "role": "skipped"},
                    {"well_name": "A2", "role": "skipped"},
                    {"well_name": "H12", "role": "skipped"},
                ],
                "all wells skipped",
            ),
        ],
    )
    def test_zero_acquisition_layout_fails_in_dry_run(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        layout_wells: list[dict[str, Any]],
        label: str,
    ) -> None:
        """A zero-acquisition layout (empty wells or every well skipped)
        must fail the job before ``dry_run_complete``."""
        method_ref, layout_ref, analysis_ref = self._stage_full(elabftw, layout_wells)
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor(job)

        self._assert_no_physical_work_or_dry_run(job, vm_agent)
        assert "zero acquired wells" in (job.error or "")

    @pytest.mark.parametrize(
        ("layout_wells", "label"),
        [
            ([], "empty wells list"),
            ([{"well_name": "A1", "role": "skipped"}], "single skipped well"),
            (
                [
                    {"well_name": "A1", "role": "skipped"},
                    {"well_name": "H12", "role": "skipped"},
                ],
                "all wells skipped",
            ),
        ],
    )
    def test_zero_acquisition_layout_fails_in_wet_path_before_run(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
        layout_wells: list[dict[str, Any]],
        label: str,
    ) -> None:
        """In the wet path, a zero-acquisition layout must fail before any
        protocol matching, cloning, run start, or MDB result fetch — even
        when a matching factory protocol exists on the instrument."""
        method_ref, layout_ref, analysis_ref = self._stage_full(elabftw, layout_wells)
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor_wet(job)

        self._assert_no_physical_work_or_dry_run(job, vm_agent)
        # No clone was attempted, so no clone event was emitted and no
        # cleanup fired.
        assert not any(e["event"] == "protocol_clone_refused" for e in job.events), job.events
        assert not any(e["event"] == "protocol_clone_failed" for e in job.events), job.events
        assert not any(e["event"] == "protocol_cloned" for e in job.events), job.events

    def test_clone_for_layout_refuses_empty_wells_directly(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """Direct contract test: :meth:`_clone_for_layout` itself must
        never return the factory ``protocol_id`` for a missing
        ``protocol_id`` or an empty ``wells`` set. It raises
        ``ValueError`` and emits ``protocol_clone_refused`` instead, so
        a future caller or an upstream-gate regression cannot reach a
        factory-protocol run through this method.
        """
        import pytest as _pytest

        job = Job(
            job_id="test-clone-refuse",
            title="Clone Refuse",
            execution_mode="generated_protocol",
            created_at="2025-01-01T00:00:00",
        )

        # Empty wells — must raise, never return the factory id.
        with _pytest.raises(ValueError, match="no acquired wells"):
            executor_wet._clone_for_layout(
                job, protocol_name="Absorbance @ 600 (1.0s)", protocol_id=1001, wells=[]
            )
        assert any(e["event"] == "protocol_clone_refused" for e in job.events), job.events
        assert "no acquired wells" in next(
            e["detail"] for e in job.events if e["event"] == "protocol_clone_refused"
        ), job.events
        # No clone was issued to the instrument.
        assert vm_agent.deleted_protocols == []

        # Missing protocol_id — must also raise, never return the (zero)
        # factory id that would resolve to nothing useful downstream.
        job.events.clear()
        with _pytest.raises(ValueError, match="no matched protocol_id"):
            executor_wet._clone_for_layout(
                job,
                protocol_name="Absorbance @ 600 (1.0s)",
                protocol_id=0,
                wells=["A1"],
            )
        assert any(e["event"] == "protocol_clone_refused" for e in job.events), job.events

    def test_nonzero_acquisition_layout_still_reaches_dry_run_complete(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Sanity: a layout with at least one acquired well (measured or
        excluded) must still proceed beyond the zero-acquisition gate to
        ``dry_run_complete`` — confirming the guard only fires on
        actually-empty acquisition."""
        method_ref, layout_ref, analysis_ref = self._stage_full(
            elabftw,
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded"},
                {"well_name": "A3", "role": "skipped"},
            ],
        )
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor(job)

        assert job.status == "completed", job.events
        assert not any(e["event"] == "layout_no_acquired_wells" for e in job.events), job.events
        assert any(e["event"] == "dry_run_complete" for e in job.events), job.events

    def test_excluded_wells_counted_as_acquired_for_zero_gate(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """An ``excluded`` well is still physically acquired (its raw value
        is collected, only analysis skips it), so a layout whose only wells
        are ``excluded`` must NOT trip the zero-acquisition gate."""
        method_ref, layout_ref, analysis_ref = self._stage_full(
            elabftw, [{"well_name": "A1", "role": "excluded"}]
        )
        job = self._make_job(method_ref, layout_ref, analysis_ref)

        executor(job)

        assert job.status == "completed", job.events
        assert any(e["event"] == "dry_run_complete" for e in job.events), job.events
        assert not any(e["event"] == "layout_no_acquired_wells" for e in job.events), job.events


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
