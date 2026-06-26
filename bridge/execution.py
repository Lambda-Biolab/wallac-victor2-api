"""Execution orchestrator for the Wallac Victor2 bridge.

Implements the execution portion of Stage 6 of
docs/plans/wallac-protocol-authoring.md.

Ties together validation, MDB generation, run execution, result
completeness checking, analysis, spool, and write-back into a single
flow.

Flow for ``generated_protocol`` mode:
1. Validate the signed bundle (Stage 4)
2. Generate the MDB protocol (Stage 5)
3. Start the run on vm-agent by AssayProtID
4. Poll for completion
5. Retrieve raw results
6. Check result completeness
7. Run analysis pipeline
8. Upload artifacts to eLabFTW (or spool on failure)
9. Write back final state

Flow for ``existing_protocol`` mode:
1. Validate (lighter — no canonical bundle)
2. Start the run by protocol name/id
3. Poll for completion
4. Retrieve raw results
5. Upload artifacts
6. Write back final state

Key safety rules:
- Never automatically repeat ambiguous physical work.
- If MDB generation succeeds but execution/analysis/write-back fails,
  preserve the generated AssayProtID, hashes, backup path, and event log.
- Route uncertain states to ``unknown_requires_operator_review``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .analysis import AnalysisPipeline, AnalysisResult
from .errors import (
    OPERATOR_REVIEW_REQUIRED,
    BridgeError,
)
from .generated_protocols import GeneratedProtocolManager
from .schemas import AnalysisSpec, ExecutionMode
from .spool import ResultSpool, compute_checksum
from .validation import ValidationService
from .vm_agent_client import VmAgentClient, VmAgentError

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Protocols for dependencies --------------------------------------------


class ElabftwWritebackClient(Protocol):
    """eLabFTW client methods needed for write-back."""

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None: ...

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]: ...

    def post_comment(self, item_id: int, comment: str) -> None: ...


# --- Execution result ------------------------------------------------------


@dataclass
class ExecutionResult:
    """Result of executing an Automation Job."""

    job_id: int
    success: bool
    final_state: str = ""  # set to completed/failed/aborted/unknown_requires_operator_review
    run_id: str = ""
    assay_prot_id: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    analysis_result: AnalysisResult | None = None
    spooled: bool = False
    error: str = ""
    events: list[dict[str, str]] = field(default_factory=list)

    def add_event(self, event: str, detail: str = "") -> None:
        self.events.append({"ts": now_iso(), "event": event, "detail": detail})


# --- Result completeness checker -------------------------------------------


def check_result_completeness(
    raw_wells: list[dict[str, Any]],
    layout_wells: dict[str, dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Check that every expected measured well has a raw result.

    Returns:
        (is_complete, list_of_issues)
    """
    issues: list[str] = []
    raw_by_name = {w["well_name"] for w in raw_wells}

    for well_name, layout_def in layout_wells.items():
        role = layout_def.get("role", "measured")
        if role == "skipped":
            # Skipped wells should NOT appear as measured
            if well_name in raw_by_name:
                issues.append(f"Skipped well {well_name} unexpectedly has a result")
            continue

        # Measured and excluded wells should have results
        if well_name not in raw_by_name:
            issues.append(f"Expected well {well_name} has no result")

    # Check for unexpected wells
    layout_names = set(layout_wells.keys())
    for well_name in raw_by_name:
        if well_name not in layout_names:
            issues.append(f"Unexpected well {well_name} in results")

    return (len(issues) == 0, issues)


# --- Execution orchestrator ------------------------------------------------


class ExecutionOrchestrator:
    """Orchestrates the full execution flow for an Automation Job.

    Depends on:
    - :class:`ValidationService` for signed bundle verification
    - :class:`GeneratedProtocolManager` for MDB protocol generation
    - :class:`VmAgentClient` for instrument communication
    - :class:`AnalysisPipeline` for result analysis
    - :class:`ResultSpool` for write-back resilience
    - eLabFTW client for write-back
    """

    def __init__(
        self,
        elabftw_client: ElabftwWritebackClient,
        vm_agent_client: VmAgentClient,
        validation_service: ValidationService,
        protocol_manager: GeneratedProtocolManager | None = None,
        analysis_pipeline: AnalysisPipeline | None = None,
        spool: ResultSpool | None = None,
        poll_interval: float = 2.0,
        poll_timeout: float = 600.0,
    ) -> None:
        self.elabftw = elabftw_client
        self.vm_agent = vm_agent_client
        self.validation = validation_service
        self.protocols = protocol_manager
        self.analysis = analysis_pipeline or AnalysisPipeline()
        self.spool = spool or ResultSpool()
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def execute_job(
        self,
        job_item_id: int,
        execution_mode: str,
        protocol_name: str = "",
        spec_dict: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Execute an Automation Job end-to-end.

        Args:
            job_item_id: eLabFTW Automation Job item ID.
            execution_mode: "generated_protocol" or "existing_protocol".
            protocol_name: Required for existing_protocol mode.
            spec_dict: The parsed job spec (for generated_protocol mode).

        Returns:
            ExecutionResult with all outcomes.
        """
        result = ExecutionResult(job_id=job_item_id, success=False)

        try:
            self._run_validation(job_item_id, result)
            if result.final_state:
                return result

            assay_prot_id = self._prepare_protocol(
                job_item_id, execution_mode, protocol_name, spec_dict, result
            )

            self._start_and_poll(job_item_id, execution_mode, assay_prot_id, protocol_name, result)
            if result.final_state in ("aborted", "failed"):
                self._write_terminal_state(job_item_id, result)
                return result

            raw_wells = self._retrieve_results(job_item_id, result.run_id, result)
            self._check_and_analyze(job_item_id, execution_mode, spec_dict, raw_wells, result)

            self._write_progress(job_item_id, 90, "Uploading artifacts")
            self._upload_or_spool(job_item_id, result.run_id, assay_prot_id, raw_wells, result)

            result.success = result.final_state not in (
                "failed",
                "aborted",
                "unknown_requires_operator_review",
            )
            if not result.final_state:
                result.final_state = "completed"
            result.add_event("execution_completed")
            self._write_terminal_state(job_item_id, result)

        except VmAgentError as e:
            result.final_state = "failed"
            result.error = f"vm-agent error: {e}"
            result.add_event("vm_agent_error", str(e))
            self._write_terminal_state(job_item_id, result)
        except BridgeError as e:
            result.final_state = "unknown_requires_operator_review"
            result.error = str(e)
            result.add_event("bridge_error", str(e))
            self._write_terminal_state(job_item_id, result)
        except Exception as e:
            result.final_state = "unknown_requires_operator_review"
            result.error = f"Unexpected error: {e}"
            result.add_event("unexpected_error", str(e))
            self._write_terminal_state(job_item_id, result)

        return result

    def _run_validation(self, job_item_id: int, result: ExecutionResult) -> None:
        """Step 1: Validate the signed bundle."""
        result.add_event("validation_started")
        self._write_state(job_item_id, "validating")
        report = self.validation.validate_job(job_item_id)
        if not report.valid:
            result.add_event("validation_failed", str(report.errors))
            result.final_state = "failed"
            result.error = f"Validation failed: {report.errors}"
            self._write_terminal_state(job_item_id, result)
            return
        result.add_event("validation_passed")

    def _prepare_protocol(
        self,
        job_item_id: int,
        execution_mode: str,
        protocol_name: str,
        spec_dict: dict[str, Any] | None,
        result: ExecutionResult,
    ) -> int:
        """Step 2: Generate MDB protocol or validate existing protocol name."""
        if execution_mode == ExecutionMode.GENERATED_PROTOCOL.value:
            if self.protocols is None:
                raise BridgeError(
                    code=OPERATOR_REVIEW_REQUIRED,
                    human_message="Generated protocol manager not configured",
                )
            result.add_event("protocol_generation_started")
            self._write_state(job_item_id, "ready")

            mode = self._extract_mode(spec_dict)
            spec_hash = self._extract_hash(spec_dict)

            proto = self.protocols.generate_protocol(job_item_id, mode, spec_hash, spec_dict or {})
            result.assay_prot_id = proto.assay_prot_id
            result.add_event("protocol_generated", f"AssayProtID={proto.assay_prot_id}")

            self.elabftw.patch_metadata(
                job_item_id,
                {
                    "Generated AssayProtID": {"value": str(proto.assay_prot_id)},
                    "MDB backup path": {"value": proto.backup_path},
                },
            )
            return proto.assay_prot_id
        else:
            if not protocol_name:
                raise BridgeError(
                    code=OPERATOR_REVIEW_REQUIRED,
                    human_message="Protocol name required for existing_protocol mode",
                )
            return 0

    def _start_and_poll(
        self,
        job_item_id: int,
        execution_mode: str,
        assay_prot_id: int,
        protocol_name: str,
        result: ExecutionResult,
    ) -> None:
        """Steps 3-4: Start the run and poll for completion."""
        result.add_event("run_starting")
        self._write_state(job_item_id, "running")

        if execution_mode == ExecutionMode.GENERATED_PROTOCOL.value:
            run_response = self.vm_agent.start_run(assay_prot_id)
        else:
            run_response = self.vm_agent.start_run(protocol_name)

        result.run_id = run_response.get("run_id", "")
        result.add_event("run_started", f"run_id={result.run_id}")

        self.elabftw.patch_metadata(
            job_item_id,
            {
                "Wallac run ID": {"value": result.run_id},
            },
        )

        self._write_progress(job_item_id, 50, "Running measurement")
        final_state = self._poll_run_completion(result.run_id, result)
        if final_state == "aborted":
            result.final_state = "aborted"
            result.add_event("run_aborted")
        elif final_state == "failed":
            result.final_state = "failed"
            result.error = "Instrument run failed"
            result.add_event("run_failed")
        else:
            result.add_event("run_completed")

    def _retrieve_results(
        self,
        job_item_id: int,
        run_id: str,
        result: ExecutionResult,
    ) -> list[dict[str, Any]]:
        """Step 5: Retrieve raw results from vm-agent."""
        self._write_progress(job_item_id, 70, "Retrieving results")
        raw_results = self.vm_agent.get_run_results(run_id)
        raw_wells = raw_results.get("wells", [])
        result.add_event("results_retrieved", f"{len(raw_wells)} wells")
        return raw_wells

    def _check_and_analyze(
        self,
        job_item_id: int,
        execution_mode: str,
        spec_dict: dict[str, Any] | None,
        raw_wells: list[dict[str, Any]],
        result: ExecutionResult,
    ) -> None:
        """Steps 6-7: Check result completeness and run analysis."""
        if not spec_dict or execution_mode != ExecutionMode.GENERATED_PROTOCOL.value:
            return

        layout_wells = self._extract_layout_wells(spec_dict)

        if layout_wells:
            complete, issues = check_result_completeness(raw_wells, layout_wells)
            if not complete:
                result.add_event("completeness_check_failed", "; ".join(issues))
                result.final_state = "unknown_requires_operator_review"
                result.error = f"Result incomplete: {issues}"
                return
            result.add_event("completeness_check_passed")

        self._write_progress(job_item_id, 80, "Analyzing results")
        analysis_spec = self._extract_analysis_spec(spec_dict)
        if analysis_spec is not None and layout_wells:
            try:
                analysis_result = self.analysis.run(raw_wells, layout_wells, analysis_spec)
                result.analysis_result = analysis_result
                result.add_event("analysis_completed", f"pass_fail={analysis_result.pass_fail}")
            except Exception as e:
                result.add_event("analysis_failed", str(e))
                result.final_state = "unknown_requires_operator_review"
                result.error = f"Analysis failed: {e}"

    # --- Internal helpers ---

    def _poll_run_completion(self, run_id: str, result: ExecutionResult) -> str:
        """Poll the vm-agent until the run completes. Returns final state."""
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            try:
                run_info = self.vm_agent.get_run(run_id)
                state = run_info.get("state", "")
                if state in ("measured", "completed"):
                    return "completed"
                if state in ("aborted", "failed"):
                    return state
            except VmAgentError as e:
                result.add_event("poll_error", str(e))
            time.sleep(self.poll_interval)
        return "failed"

    def _upload_or_spool(
        self,
        job_id: int,
        run_id: str,
        assay_prot_id: int,
        raw_wells: list[dict[str, Any]],
        result: ExecutionResult,
    ) -> None:
        """Upload artifacts to eLabFTW, or spool on failure."""
        artifacts = self._build_artifacts(raw_wells, result)

        try:
            for artifact in artifacts:
                self.elabftw.upload_file(
                    job_id,
                    artifact["filename"],
                    artifact["content"],
                    comment=artifact.get("comment", ""),
                )
                result.add_event("artifact_uploaded", artifact["filename"])
            result.spooled = False
        except Exception as e:
            logger.warning("eLabFTW write-back failed, spooling: %s", e)
            result.add_event("writeback_failed_spooling", str(e))
            self.spool.spool_results(
                job_id=job_id,
                run_id=run_id,
                assay_prot_id=assay_prot_id,
                artifacts=artifacts,
                analysis_summary=result.analysis_result.summary if result.analysis_result else {},
            )
            result.spooled = True

    def _build_artifacts(
        self, raw_wells: list[dict[str, Any]], result: ExecutionResult
    ) -> list[dict[str, Any]]:
        """Build the list of artifacts to upload/spool."""
        artifacts: list[dict[str, Any]] = []

        # Raw results JSON
        raw_json = json.dumps(raw_wells, sort_keys=True, separators=(",", ":")).encode()
        artifacts.append(
            {
                "filename": "raw_results.json",
                "content": raw_json,
                "checksum": compute_checksum(raw_json),
                "comment": "Raw per-well results from instrument",
            }
        )

        # Analysis artifacts (if available)
        if result.analysis_result is not None:
            ar = result.analysis_result

            analyzed_csv = ar.to_analyzed_wells_csv().encode()
            artifacts.append(
                {
                    "filename": "analyzed_wells.csv",
                    "content": analyzed_csv,
                    "checksum": compute_checksum(analyzed_csv),
                    "comment": "Analyzed per-well results",
                }
            )

            rep_csv = ar.to_replicate_summary_csv().encode()
            artifacts.append(
                {
                    "filename": "replicate_summary.csv",
                    "content": rep_csv,
                    "checksum": compute_checksum(rep_csv),
                    "comment": "Replicate group summary",
                }
            )

            rep_json = ar.to_replicate_summary_json().encode()
            artifacts.append(
                {
                    "filename": "replicate_summary.json",
                    "content": rep_json,
                    "checksum": compute_checksum(rep_json),
                    "comment": "Replicate group summary (JSON)",
                }
            )

            summary_json = ar.to_analysis_summary_json().encode()
            artifacts.append(
                {
                    "filename": "analysis_summary.json",
                    "content": summary_json,
                    "checksum": compute_checksum(summary_json),
                    "comment": "Analysis summary",
                }
            )

        return artifacts

    def _extract_mode(self, spec_dict: dict[str, Any] | None) -> str:
        """Extract measurement mode from job spec."""
        if not spec_dict:
            return "photometry"
        method = spec_dict.get("method", {})
        return method.get("mode", "photometry")

    def _extract_hash(self, spec_dict: dict[str, Any] | None) -> str:
        """Extract spec hash from job spec."""
        if not spec_dict:
            return ""
        return spec_dict.get("hash", "")

    def _extract_layout_wells(self, spec_dict: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Extract layout wells from job spec dict."""
        # In production, this would download and parse the layout.json
        # attachment. For now, return empty if not inline.
        layout = spec_dict.get("layout", {})
        if isinstance(layout, dict) and "wells" in layout:
            return {w["well_name"]: w for w in layout["wells"]}
        return {}

    def _extract_analysis_spec(self, spec_dict: dict[str, Any]) -> AnalysisSpec | None:
        """Extract analysis spec from job spec dict."""
        analysis = spec_dict.get("analysis", {})
        if not analysis:
            return None
        try:
            return AnalysisSpec.from_dict(analysis)
        except Exception as e:
            logger.warning("Failed to parse analysis spec: %s", e)
            return None

    def _write_state(self, job_id: int, state: str) -> None:
        """Write state to eLabFTW metadata."""
        try:
            self.elabftw.patch_metadata(
                job_id,
                {
                    "Automation state": {"value": state},
                },
            )
        except Exception as e:
            logger.warning("Failed to write state '%s' for job %d: %s", state, job_id, e)

    def _write_progress(self, job_id: int, percent: int, step: str) -> None:
        """Write progress to eLabFTW metadata."""
        try:
            self.elabftw.patch_metadata(
                job_id,
                {
                    "Progress percent": {"value": str(percent)},
                    "Current step": {"value": step},
                    "Last heartbeat": {"value": now_iso()},
                },
            )
        except Exception as e:
            logger.warning("Failed to write progress for job %d: %s", job_id, e)

    def _write_terminal_state(self, job_id: int, result: ExecutionResult) -> None:
        """Write final state to eLabFTW metadata."""
        try:
            fields: dict[str, Any] = {
                "Automation state": {
                    "value": "completed" if result.success else result.final_state
                },
                "Final state": {"value": result.final_state},
                "Last heartbeat": {"value": now_iso()},
            }
            if result.error:
                fields["Last error code"] = {"value": "execution_failed"}
                fields["Operator hint"] = {"value": result.error}
            if result.run_id:
                fields["Wallac run ID"] = {"value": result.run_id}
            if result.assay_prot_id:
                fields["Generated AssayProtID"] = {"value": str(result.assay_prot_id)}
            if result.spooled:
                fields["Last error code"] = {"value": "writeback_spooled"}
                fields["Operator hint"] = {
                    "value": "Results spooled locally — retry write-back pending"
                }

            self.elabftw.patch_metadata(job_id, fields)

            # Post event log as comment
            if result.events:
                event_log = "\n".join(
                    f"[{e['ts']}] {e['event']}: {e['detail']}" for e in result.events
                )
                self.elabftw.post_comment(job_id, f"Execution event log:\n{event_log}")
        except Exception as e:
            logger.error("Failed to write terminal state for job %d: %s", job_id, e)
