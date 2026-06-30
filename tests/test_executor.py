"""Tests for the BridgeExecutor — direct-submit execution with hash verification.

C20 requires signed-spec hash verification in the executor path.
Tests cover:
- Valid signed ref with correct hash passes verification
- Valid ref executes through existing generated_protocol path (dry-run)
- Hash mismatch blocks execution (fails closed)
- Missing hash in ref blocks execution (fails closed)
- Missing object_id/attachment_id blocks execution (fails closed)
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bridge.canonical import canonicalize_and_hash, compute_hash
from bridge.errors import CANONICAL_HASH_MISMATCH, SIGNATURE_MISSING, BridgeError
from bridge.executor import BridgeExecutor
from bridge.jobs import Job

# --- Mock clients ---


class MockElabftwClient:
    """In-memory mock for eLabFTW client in executor tests."""

    def __init__(self) -> None:
        self._uploads: dict[tuple[int, int], bytes] = {}
        self._experiments: dict[int, dict[str, Any]] = {}
        self._next_exp_id = 1

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
        return {"id": exp_id, "real_name": filename}

    def patch_experiment(self, exp_id: int, data: dict[str, Any]) -> None:
        self._experiments[exp_id].update(data)


class MockVmAgentClient:
    """Mock vm-agent for executor tests."""

    def __init__(self) -> None:
        self._protocols: list[dict[str, Any]] = []
        self._runs: dict[str, dict[str, Any]] = {}
        self._next_run = 1

    def add_protocol(self, proto: dict[str, Any]) -> None:
        self._protocols.append(proto)

    def get_protocols(self, refresh: bool = False) -> list[dict[str, Any]]:
        return self._protocols

    def get_protocol(self, name_or_id: str | int) -> dict[str, Any]:
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
        return {"run_id": run_id, "state": "running", "protocol_id": protocol}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id, {})
        if run.get("state") == "running":
            run["state"] = "measured"
        return run

    def get_run_results(
        self, run_id: str, shape: str = "list", value: str = "od", dedup: bool = True
    ) -> dict[str, Any]:
        return {"run_id": run_id, "well_count": 0, "wells": []}

    def clone_protocol(self, template_id: int, new_id: int, name: str) -> None:
        pass

    def update_plate_map(self, protocol_id: int, wells: list[str]) -> None:
        pass

    def delete_protocol(self, protocol_id: int) -> None:
        pass

    def abort_run(self, run_id: str) -> None:
        pass


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


# --- Test: _download_ref hash verification (C20) ---


class TestDownloadRefHashVerification:
    """Direct tests of BridgeExecutor._download_ref hash verification."""

    def test_valid_ref_passes(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A ref with correct hash downloads and parses successfully."""
        spec_dict = {"schema_name": "wallac.method", "schema_version": 1}
        spec_hash = compute_hash(json.dumps(spec_dict).encode())
        # Use utf-8 canonical encoding matching executor's actual flow
        spec_bytes = json.dumps(spec_dict).encode()
        elabftw.add_upload(42, 5001, spec_bytes)

        ref = {
            "object_id": 42,
            "hash": spec_hash,
            "json_attachment_id": 5001,
        }
        result = executor._download_ref(ref)
        assert result == spec_dict

    def test_valid_ref_with_legacy_attachment_id(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Backward compatibility: ref with 'attachment_id' key (not json_attachment_id)."""
        spec_dict = {"schema_name": "wallac.method", "schema_version": 1}
        spec_bytes = json.dumps(spec_dict).encode()
        spec_hash = compute_hash(spec_bytes)
        elabftw.add_upload(42, 5001, spec_bytes)

        ref = {
            "object_id": 42,
            "hash": spec_hash,
            "attachment_id": 5001,
        }
        result = executor._download_ref(ref)
        assert result == spec_dict

    def test_hash_mismatch_raises(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Hash mismatch raises CANONICAL_HASH_MISMATCH BridgeError."""
        original = {"schema_name": "wallac.method", "schema_version": 1}
        tampered = {"schema_name": "wallac.method", "schema_version": 1, "name": "TAMPERED"}
        original_bytes = json.dumps(original).encode()
        tampered_bytes = json.dumps(tampered).encode()
        wrong_hash = compute_hash(tampered_bytes)

        elabftw.add_upload(42, 5001, original_bytes)

        ref = {
            "object_id": 42,
            "hash": wrong_hash,
            "json_attachment_id": 5001,
        }

        with pytest.raises(BridgeError) as exc_info:
            executor._download_ref(ref)

        assert exc_info.value.code == CANONICAL_HASH_MISMATCH
        assert "hash mismatch" in exc_info.value.human_message.lower()
        expected_details = exc_info.value.details
        assert expected_details["expected_hash"] == wrong_hash
        assert expected_details["actual_hash"] == compute_hash(original_bytes)

    def test_missing_hash_in_ref_raises(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Missing 'hash' field in ref raises SIGNATURE_MISSING BridgeError."""
        elabftw.add_upload(42, 5001, b"{}")
        ref = {
            "object_id": 42,
            "json_attachment_id": 5001,
            # no "hash" key
        }

        with pytest.raises(BridgeError) as exc_info:
            executor._download_ref(ref)

        assert exc_info.value.code == SIGNATURE_MISSING
        assert "hash" in exc_info.value.human_message.lower()

    def test_missing_object_id_raises(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Missing object_id in ref raises SIGNATURE_MISSING BridgeError."""
        ref = {
            "hash": "abc123",
            "json_attachment_id": 5001,
            # no "object_id"
        }

        with pytest.raises(BridgeError) as exc_info:
            executor._download_ref(ref)

        assert exc_info.value.code == SIGNATURE_MISSING

    def test_missing_attachment_id_raises(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Missing json_attachment_id and attachment_id in ref raises SIGNATURE_MISSING."""
        ref = {
            "object_id": 42,
            "hash": "abc123",
            # no attachment id at all
        }

        with pytest.raises(BridgeError) as exc_info:
            executor._download_ref(ref)

        assert exc_info.value.code == SIGNATURE_MISSING

    def test_empty_ref_dict_raises(
        self,
        executor: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """An empty ref dict raises SIGNATURE_MISSING."""
        with pytest.raises(BridgeError) as exc_info:
            executor._download_ref({})

        assert exc_info.value.code == SIGNATURE_MISSING


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

    def test_valid_ref_executes(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """A valid signed ref passes verification and proceeds to execution.

        For generated_protocol mode, the executor validates method_ref hash
        before attempting any protocol matching. With dry_run=False but a
        matching protocol available, it should proceed to protocol cloning
        and run start.
        """
        method_spec, method_bytes, method_hash = self._make_method_spec()
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

    def test_hash_mismatch_blocks_execution(
        self,
        executor_wet: BridgeExecutor,
        elabftw: MockElabftwClient,
    ) -> None:
        """Hash mismatch blocks execution — job is failed with appropriate error."""
        method_spec, method_bytes, method_hash = self._make_method_spec()
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
        method_spec, method_bytes, method_hash = self._make_method_spec()
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
        method_spec, method_bytes, method_hash = self._make_method_spec()
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
        method_spec, method_bytes, method_hash = self._make_method_spec()
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
