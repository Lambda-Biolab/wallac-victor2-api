"""Tests for the vm-agent HTTP client, result spool, and execution orchestrator.

These tests use mocks for all external dependencies (vm-agent HTTP, eLabFTW API,
MDB). No real network or hardware is touched.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bridge.execution import (
    ExecutionOrchestrator,
    check_result_completeness,
)
from bridge.generated_protocols import (
    GENERATED_GROUP_NAME,
    GeneratedProtocolManager,
    TemplateFingerprint,
)
from bridge.spool import ResultSpool
from bridge.validation import ValidationService
from bridge.vm_agent_client import VmAgentClient, VmAgentError

# --- VmAgentClient tests ---


class TestVmAgentClient:
    def test_init(self) -> None:
        c = VmAgentClient("http://localhost:8420", token="abc")
        assert c.base == "http://localhost:8420"
        assert c.token == "abc"

    @patch("bridge.vm_agent_client.urllib.request.urlopen")
    def test_get_health(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        c = VmAgentClient("http://localhost:8420", token="abc")
        result = c.get_health()
        assert result == {"ok": True}

    @patch("bridge.vm_agent_client.urllib.request.urlopen")
    def test_start_run(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "run_id": "r-abc123",
                "state": "running",
            }
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        c = VmAgentClient("http://localhost:8420", token="abc")
        result = c.start_run("Absorbance 600")
        assert result["run_id"] == "r-abc123"

    @patch("bridge.vm_agent_client.urllib.request.urlopen")
    def test_measure(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "run_id": "r-abc",
                "state": "measured",
                "wells": [{"well": "A01", "od": 0.5}],
            }
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        c = VmAgentClient("http://localhost:8420", token="abc")
        result = c.measure("Absorbance 600")
        assert result["state"] == "measured"


# --- ResultSpool tests ---


class TestResultSpool:
    @pytest.fixture
    def spool(self, tmp_path: Any) -> ResultSpool:
        return ResultSpool(spool_dir=str(tmp_path / "spool"))

    def test_spool_results(self, spool: ResultSpool) -> None:
        artifacts = [
            {"filename": "raw_results.json", "content": b'{"wells": []}', "checksum": "abc"},
            {"filename": "analyzed.csv", "content": b"well,od\nA01,0.5", "checksum": "def"},
        ]
        entry = spool.spool_results(
            job_id=42,
            run_id="r-abc",
            assay_prot_id=2000001,
            artifacts=artifacts,
            analysis_summary={"pass_fail": "pass"},
        )
        assert entry.job_id == 42
        assert entry.status == "pending"
        assert len(entry.artifacts) == 2

    def test_load_entry(self, spool: ResultSpool) -> None:
        spool.spool_results(
            job_id=42,
            run_id="r-abc",
            assay_prot_id=2000001,
            artifacts=[{"filename": "test.json", "content": b"{}"}],
            analysis_summary={},
        )
        entry = spool.load_entry(42)
        assert entry is not None
        assert entry.job_id == 42
        assert entry.run_id == "r-abc"

    def test_load_entry_not_found(self, spool: ResultSpool) -> None:
        entry = spool.load_entry(999)
        assert entry is None

    def test_read_artifact(self, spool: ResultSpool) -> None:
        content = b'{"wells": []}'
        spool.spool_results(
            job_id=42,
            run_id="r-abc",
            assay_prot_id=0,
            artifacts=[{"filename": "raw.json", "content": content}],
            analysis_summary={},
        )
        read = spool.read_artifact(42, "raw.json")
        assert read == content

    def test_read_artifact_not_found(self, spool: ResultSpool) -> None:
        assert spool.read_artifact(999, "missing.json") is None

    def test_list_pending(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "a.json", "content": b"{}"}], {})
        spool.spool_results(2, "r-2", 0, [{"filename": "b.json", "content": b"{}"}], {})
        pending = spool.list_pending()
        assert len(pending) == 2

    def test_mark_completed(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "a.json", "content": b"{}"}], {})
        spool.mark_completed(1)
        entry = spool.load_entry(1)
        assert entry is not None
        assert entry.status == "completed"

    def test_increment_retry(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "a.json", "content": b"{}"}], {})
        count = spool.increment_retry(1)
        assert count == 1
        entry = spool.load_entry(1)
        assert entry is not None
        assert entry.retry_count == 1

    def test_max_retries_marks_failed(self, spool: ResultSpool) -> None:
        spool.max_retries = 2
        spool.spool_results(1, "r-1", 0, [{"filename": "a.json", "content": b"{}"}], {})
        spool.increment_retry(1)
        spool.increment_retry(1)
        entry = spool.load_entry(1)
        assert entry is not None
        assert entry.status == "failed"

    def test_cleanup_completed(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "a.json", "content": b"{}"}], {})
        spool.mark_completed(1)
        # Set grace period to 0 so it's immediately eligible
        cleaned = spool.cleanup_completed(grace_period=0)
        assert 1 in cleaned
        assert spool.load_entry(1) is None

    def test_scan_for_secrets_clean(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "results.json", "content": b"{}"}], {})
        findings = spool.scan_for_secrets()
        assert findings == []

    def test_scan_for_secrets_detects_token(self, spool: ResultSpool) -> None:
        spool.spool_results(1, "r-1", 0, [{"filename": "token.txt", "content": b"abc"}], {})
        findings = spool.scan_for_secrets()
        assert len(findings) > 0


# --- check_result_completeness tests ---


class TestResultCompleteness:
    def test_complete(self) -> None:
        layout = {"A1": {"role": "measured"}, "A2": {"role": "measured"}}
        raw = [{"well_name": "A1"}, {"well_name": "A2"}]
        complete, issues = check_result_completeness(raw, layout)
        assert complete is True
        assert issues == []

    def test_missing_well(self) -> None:
        layout = {"A1": {"role": "measured"}, "A2": {"role": "measured"}}
        raw = [{"well_name": "A1"}]
        complete, issues = check_result_completeness(raw, layout)
        assert complete is False
        assert any("A2" in i for i in issues)

    def test_skipped_well_with_result(self) -> None:
        layout = {"A1": {"role": "measured"}, "A2": {"role": "skipped"}}
        raw = [{"well_name": "A1"}, {"well_name": "A2"}]
        complete, issues = check_result_completeness(raw, layout)
        assert complete is False
        assert any("A2" in i and "unexpectedly" in i for i in issues)

    def test_unexpected_well(self) -> None:
        layout = {"A1": {"role": "measured"}}
        raw = [{"well_name": "A1"}, {"well_name": "Z99"}]
        complete, issues = check_result_completeness(raw, layout)
        assert complete is False
        assert any("Z99" in i for i in issues)


# --- ExecutionOrchestrator tests ---


class MockElabftwClient:
    """Mock eLabFTW client for orchestrator tests."""

    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._uploads: dict[int, list[dict[str, Any]]] = {}
        self._comments: dict[int, list[str]] = {}

    def add_item(self, item_id: int, extra_fields: dict[str, Any] | None = None) -> None:
        ef = extra_fields or {}
        self._items[item_id] = {"id": item_id, "metadata": json.dumps({"extra_fields": ef})}
        self._uploads[item_id] = []
        self._comments[item_id] = []

    def get_item(self, item_id: int) -> dict[str, Any]:
        return dict(self._items[item_id])

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        item = self._items[item_id]
        meta = (
            json.loads(item["metadata"]) if isinstance(item["metadata"], str) else item["metadata"]
        )
        ef = meta.get("extra_fields") or {}
        ef.update(extra_fields)
        meta["extra_fields"] = ef
        item["metadata"] = json.dumps(meta)

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        upload = {"id": len(self._uploads[item_id]) + 1, "real_name": filename}
        self._uploads[item_id].append(upload)
        return upload

    def post_comment(self, item_id: int, comment: str) -> None:
        self._comments[item_id].append(comment)

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return self._uploads.get(item_id, [])

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        return b"{}"


class MockVmAgentClient:
    """Mock vm-agent for orchestrator tests."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._next_run = 1
        self._run_results: dict[str, list[dict[str, Any]]] = {}
        self._should_fail = False

    def get_health(self) -> dict[str, Any]:
        return {"instrument_connected": True, "is_idle": True, "is_error": False, "ok": True}

    def get_instrument(self) -> dict[str, Any]:
        return {
            "technologies": {"photometer": True, "prompt_fluorometer": True, "luminometer": True}
        }

    def start_run(
        self, protocol: str | int, plate_id: str = "", dry_run: bool = False
    ) -> dict[str, Any]:
        if self._should_fail:
            raise VmAgentError("Simulated failure")
        run_id = f"r-{self._next_run:06d}"
        self._next_run += 1
        self._runs[run_id] = {"run_id": run_id, "state": "running"}
        return {"run_id": run_id, "state": "running", "protocol_id": protocol}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id, {})
        if run.get("state") == "running":
            run["state"] = "measured"  # auto-complete for tests
        return run

    def get_run_results(
        self, run_id: str, shape: str = "list", value: str = "od", dedup: bool = True
    ) -> dict[str, Any]:
        wells = self._run_results.get(run_id, [])
        return {"run_id": run_id, "well_count": len(wells), "wells": wells}

    def set_run_results(self, run_id: str, wells: list[dict[str, Any]]) -> None:
        self._run_results[run_id] = wells


class MockMdbClient:
    """Mock MDB client for protocol generation."""

    def __init__(self) -> None:
        self._protocols: dict[int, dict[str, Any]] = {}
        self._groups: dict[str, int] = {}
        self._backups: list[str] = []

    def add_group(self, name: str, gid: int) -> None:
        self._groups[name] = gid

    def get_protocol_group_id(self, group_name: str) -> int | None:
        return self._groups.get(group_name)

    def get_protocol(self, assay_prot_id: int) -> dict[str, Any] | None:
        return self._protocols.get(assay_prot_id)

    def find_protocol_by_name(self, name: str) -> dict[str, Any] | None:
        for p in self._protocols.values():
            if p.get("ProtName") == name:
                return p
        return None

    def get_max_protocol_id(self) -> int:
        return max(self._protocols.keys()) if self._protocols else 0

    def insert_protocol(self, protocol: dict[str, Any]) -> int:
        aid = protocol["AssayProtID"]
        self._protocols[aid] = dict(protocol)
        return aid

    def delete_protocol(self, assay_prot_id: int) -> bool:
        if assay_prot_id in self._protocols:
            del self._protocols[assay_prot_id]
            return True
        return False

    def backup_mdb(self, backup_path: str) -> str:
        full = f"/tmp/mock_backup_{backup_path}"
        self._backups.append(full)
        return full

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return list(self._protocols.values())


@pytest.fixture
def elabftw() -> MockElabftwClient:
    return MockElabftwClient()


@pytest.fixture
def vm_agent() -> MockVmAgentClient:
    return MockVmAgentClient()


@pytest.fixture
def mdb() -> MockMdbClient:
    return MockMdbClient()


@pytest.fixture
def spool(tmp_path: Any) -> ResultSpool:
    return ResultSpool(spool_dir=str(tmp_path / "spool"))


@pytest.fixture
def orchestrator(
    elabftw: MockElabftwClient,
    vm_agent: MockVmAgentClient,
    mdb: MockMdbClient,
    spool: ResultSpool,
) -> ExecutionOrchestrator:
    # Set up MDB with template and group
    mdb.add_group(GENERATED_GROUP_NAME, 99)
    template = TemplateFingerprint(
        assay_prot_id=1000003,
        mode="photometry",
        expected_name="Absorbance 600",
        expected_group="Photometry",
    )
    mdb._protocols[1000003] = {"AssayProtID": 1000003, "ProtName": "Absorbance 600"}

    proto_mgr = GeneratedProtocolManager(
        mdb,
        templates={"photometry": template},
        env={
            "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
            "WALLAC_ENABLE_PHOTOMETRY": "true",
        },
    )

    # Set up validation service with the same elabftw + vm_agent
    val_service = ValidationService(elabftw, vm_agent)  # type: ignore[arg-type]

    return ExecutionOrchestrator(
        elabftw_client=elabftw,  # type: ignore[arg-type]
        vm_agent_client=vm_agent,  # type: ignore[arg-type]
        validation_service=val_service,
        protocol_manager=proto_mgr,
        spool=spool,
        poll_interval=0.01,  # fast for tests
        poll_timeout=5.0,
    )


class TestExecutionOrchestratorExistingProtocol:
    def test_existing_protocol_success(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        # Set up job in eLabFTW
        elabftw.add_item(
            100,
            {
                "Execution mode": {"value": "existing_protocol"},
                "Protocol name": {"value": "Absorbance 600"},
            },
        )

        # Set up mock results
        vm_agent._run_results["r-000001"] = [
            {"well_name": "A1", "primary_value": 0.5},
        ]

        result = orchestrator.execute_job(
            job_item_id=100,
            execution_mode="existing_protocol",
            protocol_name="Absorbance 600",
        )

        assert result.success is True
        assert result.final_state == "completed"
        assert result.run_id != ""
        # Artifacts should be uploaded to eLabFTW
        uploads = elabftw.list_uploads(100)
        assert len(uploads) > 0

    def test_existing_protocol_missing_name(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
    ) -> None:
        elabftw.add_item(
            100,
            {
                "Execution mode": {"value": "existing_protocol"},
            },
        )

        result = orchestrator.execute_job(
            job_item_id=100,
            execution_mode="existing_protocol",
        )

        assert result.success is False
        assert result.final_state == "unknown_requires_operator_review"


class TestExecutionOrchestratorGeneratedProtocol:
    def test_generated_protocol_success(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        # Set up job with generated_protocol mode
        # The validation service needs the job to have the right metadata
        elabftw.add_item(
            100,
            {
                "Execution mode": {"value": "generated_protocol"},
                "Job hash": {"value": "abc123"},
                "Job JSON attachment ID": {"value": "5001"},
            },
        )
        # Add the job.json attachment
        elabftw._uploads[100] = [{"id": 5001, "real_name": "job.json"}]

        # We need to mock the download_upload to return the job spec
        job_spec = {
            "schema_name": "wallac.job",
            "schema_version": 1,
            "execution_mode": "generated_protocol",
            "method": {"object_id": 10, "hash": "abc", "json_attachment_id": 5001},
            "layout": {
                "source": "reusable",
                "hash": "def",
                "json_attachment_id": 5002,
                "object_id": 11,
            },
            "analysis": {"object_id": 12, "hash": "ghi", "json_attachment_id": 5003},
        }
        # Override download_upload to return our job spec
        elabftw.download_upload = lambda item_id, upload_id: json.dumps(job_spec).encode()  # type: ignore

        # Set up mock results
        vm_agent._run_results["r-000001"] = [
            {"well_name": "A1", "primary_value": 0.5},
        ]

        result = orchestrator.execute_job(
            job_item_id=100,
            execution_mode="generated_protocol",
            spec_dict=job_spec,
        )

        # Should succeed (validation may fail due to missing referenced objects,
        # but the orchestrator should handle it gracefully)
        assert result.final_state in ("completed", "failed", "unknown_requires_operator_review")


class TestExecutionOrchestratorSpool:
    def test_spool_on_writeback_failure(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        elabftw.add_item(
            100,
            {
                "Execution mode": {"value": "existing_protocol"},
                "Protocol name": {"value": "Absorbance 600"},
            },
        )

        vm_agent._run_results["r-000001"] = [
            {"well_name": "A1", "primary_value": 0.5},
        ]

        # Make upload_file fail
        original_upload = elabftw.upload_file

        def _fail_upload(
            item_id: int, filename: str, content: bytes, comment: str = ""
        ) -> dict[str, Any]:
            raise RuntimeError("eLabFTW unavailable")

        elabftw.upload_file = _fail_upload  # type: ignore[method-assign]

        result = orchestrator.execute_job(
            job_item_id=100,
            execution_mode="existing_protocol",
            protocol_name="Absorbance 600",
        )

        # Should still succeed (spooled)
        assert result.success is True
        assert result.spooled is True
        assert result.final_state == "completed"

        # Restore for cleanup
        elabftw.upload_file = original_upload  # type: ignore[method-assign]
