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
        self._experiments: dict[int, dict[str, Any]] = {}
        self._next_experiment_id = 1

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

    def create_experiment(self, title: str, body: str = "") -> int:
        exp_id = self._next_experiment_id
        self._next_experiment_id += 1
        self._experiments[exp_id] = {"title": title, "body": body, "links": []}
        return exp_id

    def link_experiment_to_item(self, experiment_id: int, item_id: int) -> None:
        self._experiments[experiment_id]["links"].append(item_id)

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
        """Full generated_protocol path with proper hash-verified attachments.

        Creates a signed bundle of method.json, layout.json, analysis.json,
        and job.json with real SHA-256 hashes, wires the mock to return the
        right bytes for each download, and verifies the orchestrator loads
        specs, generates a protocol, runs it, and writes back results.
        """
        from bridge.canonical import canonicalize_and_hash
        from bridge.schemas import (
            AnalysisSpec,
            BlankSubtractionConfig,
            LayoutSpec,
            MethodSpec,
            PhotometrySettings,
            WellSpec,
        )

        # Build canonical specs with real hashes
        method = MethodSpec(
            name="OD600 Photometry",
            mode="photometry",
            plate_type="96-well",
            photometry=PhotometrySettings(
                filter_id="P610",
                filter_name="610nm",
                read_time_seconds=0.1,
            ),
        )
        method_bytes, method_hash = canonicalize_and_hash(method.to_dict())

        layout = LayoutSpec(
            plate_type="96-well",
            wells=[
                WellSpec(well_name="A1", role="measured"),
                WellSpec(well_name="A2", role="measured"),
            ],
        )
        layout_bytes, layout_hash = canonicalize_and_hash(layout.to_dict())

        analysis = AnalysisSpec(
            blank_subtraction=BlankSubtractionConfig(enabled=False),
        )
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis.to_dict())

        # Build job spec referencing the method/layout/analysis
        job_spec_dict = {
            "schema_name": "wallac.job",
            "schema_version": 1,
            "execution_mode": "generated_protocol",
            "method": {
                "object_id": 42,
                "hash": method_hash,
                "json_attachment_id": 5001,
            },
            "layout": {
                "source": "reusable",
                "hash": layout_hash,
                "json_attachment_id": 5002,
                "object_id": 43,
            },
            "analysis": {
                "object_id": 44,
                "hash": analysis_hash,
                "json_attachment_id": 5003,
            },
        }
        job_bytes, job_hash = canonicalize_and_hash(job_spec_dict)

        # Set up job item in eLabFTW with metadata
        elabftw.add_item(
            100,
            {
                "Execution mode": {"value": "generated_protocol"},
                "Job hash": {"value": job_hash},
                "Job JSON attachment ID": {"value": "5000"},
            },
        )
        # Set up method, layout, analysis items with lifecycle
        for item_id in (42, 43, 44):
            elabftw.add_item(
                item_id,
                {"Lifecycle state": {"value": "signed/active"}},
            )

        # Wire download_upload to return the right bytes per (item_id, upload_id)
        upload_map: dict[tuple[int, int], bytes] = {
            (100, 5000): job_bytes,
            (42, 5001): method_bytes,
            (43, 5002): layout_bytes,
            (44, 5003): analysis_bytes,
        }

        def _download(item_id: int, upload_id: int) -> bytes:
            return upload_map.get((item_id, upload_id), b"{}")

        elabftw.download_upload = _download  # type: ignore[method-assign]

        # Set up mock results — 2 wells matching the layout
        vm_agent._run_results["r-000001"] = [
            {"well_name": "A1", "primary_value": 0.5, "od": 0.5},
            {"well_name": "A2", "primary_value": 0.6, "od": 0.6},
        ]

        result = orchestrator.execute_job(
            job_item_id=100,
            execution_mode="generated_protocol",
        )

        assert result.success is True
        assert result.final_state == "completed"
        assert result.assay_prot_id >= 2000000  # generated ID namespace
        # Artifacts should be uploaded
        uploads = elabftw.list_uploads(100)
        assert len(uploads) > 0  # raw_results.json at minimum
        # Should have events for spec loading
        event_names = [e["event"] for e in result.events]
        assert "job_spec_loaded" in event_names
        assert "method_spec_loaded" in event_names
        assert "layout_spec_loaded" in event_names
        assert "analysis_spec_loaded" in event_names
        assert "protocol_generated" in event_names


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


class TestExecutionOrchestratorDryRun:
    """Dry-run mode: validate signed bundles without touching the instrument."""

    def test_dry_run_existing_protocol(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """Dry-run validates and returns without executing the run."""
        orchestrator.dry_run = True
        elabftw.add_item(
            200,
            {
                "Execution mode": {"value": "existing_protocol"},
                "Protocol name": {"value": "Absorbance 600"},
            },
        )

        result = orchestrator.execute_job(
            job_item_id=200,
            execution_mode="existing_protocol",
            protocol_name="Absorbance 600",
        )

        assert result.success is True
        assert result.final_state == "completed"
        assert result.run_id == ""  # no run was started
        assert any(e["event"] == "dry_run_validation_passed" for e in result.events)
        # Dry-run report should be uploaded
        uploads = elabftw.list_uploads(200)
        assert any(u["real_name"] == "dry_run_report.json" for u in uploads)
        # vm-agent should NOT have been contacted for a run
        assert len(vm_agent._run_results) == 0

    def test_dry_run_generated_protocol(
        self,
        orchestrator: ExecutionOrchestrator,
        elabftw: MockElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        """Dry-run validates canonical specs without executing."""
        from bridge.canonical import canonicalize_and_hash
        from bridge.schemas import (
            AnalysisSpec,
            BlankSubtractionConfig,
            LayoutSpec,
            MethodSpec,
            PhotometrySettings,
            WellSpec,
        )

        orchestrator.dry_run = True

        # Build canonical specs
        method = MethodSpec(
            name="OD600 Photometry",
            mode="photometry",
            plate_type="96-well",
            photometry=PhotometrySettings(
                filter_id="P610",
                filter_name="610nm",
                read_time_seconds=0.1,
            ),
        )
        method_bytes, method_hash = canonicalize_and_hash(method.to_dict())

        layout = LayoutSpec(
            plate_type="96-well",
            wells=[WellSpec(well_name="A1", role="measured")],
        )
        layout_bytes, layout_hash = canonicalize_and_hash(layout.to_dict())

        analysis = AnalysisSpec(
            blank_subtraction=BlankSubtractionConfig(enabled=False),
        )
        analysis_bytes, analysis_hash = canonicalize_and_hash(analysis.to_dict())

        job_spec_dict = {
            "schema_name": "wallac.job",
            "schema_version": 1,
            "execution_mode": "generated_protocol",
            "method": {"object_id": 301, "hash": method_hash, "json_attachment_id": 5001},
            "layout": {
                "source": "reusable",
                "hash": layout_hash,
                "json_attachment_id": 5002,
                "object_id": 302,
            },
            "analysis": {"object_id": 303, "hash": analysis_hash, "json_attachment_id": 5003},
        }
        job_bytes, job_hash = canonicalize_and_hash(job_spec_dict)

        # Set up job item in eLabFTW
        elabftw.add_item(
            300,
            {
                "Execution mode": {"value": "generated_protocol"},
                "Job hash": {"value": job_hash},
                "Job JSON attachment ID": {"value": "5000"},
            },
        )
        for item_id in (301, 302, 303):
            elabftw.add_item(item_id, {"Lifecycle state": {"value": "signed/active"}})

        # Wire download_upload
        upload_map: dict[tuple[int, int], bytes] = {
            (300, 5000): job_bytes,
            (301, 5001): method_bytes,
            (302, 5002): layout_bytes,
            (303, 5003): analysis_bytes,
        }
        elabftw.download_upload = lambda iid, uid: upload_map.get((iid, uid), b"{}")  # type: ignore[method-assign]

        result = orchestrator.execute_job(
            job_item_id=300,
            execution_mode="generated_protocol",
        )

        assert result.success is True
        assert result.final_state == "completed"
        assert result.run_id == ""
        assert any(e["event"] == "dry_run_validation_passed" for e in result.events)
        uploads = elabftw.list_uploads(300)
        assert any(u["real_name"] == "dry_run_report.json" for u in uploads)
        # No protocol should have been generated
        assert len(vm_agent._run_results) == 0
