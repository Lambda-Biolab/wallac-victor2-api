"""Bridge executor for the direct-submit model.

Connects the :class:`~bridge.jobs.JobManager` to the vm-agent and eLabFTW.
When a job is submitted via HTTP POST, the JobManager queues it and the
background worker calls this executor.

For ``existing_protocol`` mode: resolve protocol by name on vm-agent,
start run, poll for completion, fetch results, write back to eLabFTW.

For ``generated_protocol`` mode: download canonical JSON specs from
eLabFTW (using method_ref/layout_ref/analysis_ref), run analysis
pipeline, write results back to eLabFTW experiment.

This is the direct-submit equivalent of :class:`ExecutionOrchestrator`
— simpler because the job spec arrives via HTTP with all refs included,
no eLabFTW polling or claiming needed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .analysis import AnalysisPipeline
from .elabftw import ElabftwClient
from .jobs import Job
from .schemas import AnalysisSpec
from .vm_agent_client import VmAgentClient, VmAgentError

logger = logging.getLogger(__name__)

# Poll interval for vm-agent run status (seconds)
POLL_INTERVAL = 2.0
# Maximum time to wait for a run to complete (seconds)
POLL_TIMEOUT = 600.0


class BridgeExecutor:
    """Executes direct-submit jobs through the vm-agent and writes results to eLabFTW.

    Set as the executor on :class:`JobManager` via ``set_executor()``.
    Called by the worker thread for each queued job.
    """

    def __init__(
        self,
        vm_agent: VmAgentClient,
        elabftw: ElabftwClient,
        dry_run: bool = False,
    ) -> None:
        self.vm_agent = vm_agent
        self.elabftw = elabftw
        self.dry_run = dry_run
        self.analysis = AnalysisPipeline()

    def __call__(self, job: Job) -> None:
        """Execute a job. Called by the JobManager worker thread."""
        if job.execution_mode == "existing_protocol":
            self._execute_existing_protocol(job)
        elif job.execution_mode == "generated_protocol":
            self._execute_generated_protocol(job)
        else:
            job.status = "failed"
            job.error = f"Unknown execution_mode: {job.execution_mode}"
            job.add_event("execution_failed", job.error)

    # --- existing_protocol mode ---

    def _execute_existing_protocol(self, job: Job) -> None:
        """Run a factory preset protocol by name."""
        protocol_name = job.protocol_name
        if not protocol_name:
            job.status = "failed"
            job.error = "No protocol_name specified for existing_protocol mode"
            job.add_event("execution_failed", job.error)
            return

        job.add_event("resolving_protocol", protocol_name)
        try:
            # Try resolving by name first; if the name contains special chars
            # that break URL paths (e.g. '/'), fall back to searching the
            # protocol list for a name match and resolve by ID.
            proto = self.vm_agent.get_protocol(protocol_name)
            job.add_event("protocol_resolved", f"id={proto.get('id')}")
        except VmAgentError as e:
            if e.status_code == 404:
                # Fallback: search protocol list for a name match
                try:
                    prots_resp = self.vm_agent.get_protocols()
                    prots = (
                        prots_resp.get("protocols", prots_resp)
                        if isinstance(prots_resp, dict)
                        else prots_resp
                    )
                    proto = next(
                        (p for p in prots if p.get("name") == protocol_name),
                        None,
                    )
                    if proto is None:
                        job.status = "failed"
                        job.error = f"Protocol '{protocol_name}' not found by name or ID"
                        job.add_event("execution_failed", job.error)
                        return
                    job.add_event("protocol_resolved", f"id={proto.get('id')} (via list search)")
                except VmAgentError as e2:
                    job.status = "failed"
                    job.error = f"Protocol '{protocol_name}' not found: {e2}"
                    job.add_event("execution_failed", job.error)
                    return
            else:
                job.status = "failed"
                job.error = f"Protocol '{protocol_name}' not found: {e}"
                job.add_event("execution_failed", job.error)
                return

        if self.dry_run:
            job.status = "completed"
            job.add_event("dry_run_complete", f"Would run protocol {protocol_name}")
            return

        # Start the run — use the protocol ID (resolved above) to avoid
        # URL path issues with names containing special characters
        proto_id = proto.get("id", protocol_name)
        job.add_event("starting_run", str(proto_id))
        try:
            run_resp = self.vm_agent.start_run(proto_id)
            run_id = run_resp.get("run_id", "")
            if not run_id:
                job.status = "failed"
                job.error = f"No run_id in response: {run_resp}"
                job.add_event("execution_failed", job.error)
                return
            job.run_id = run_id
            job.add_event("run_started", run_id)
        except VmAgentError as e:
            job.status = "failed"
            job.error = f"Failed to start run: {e}"
            job.add_event("execution_failed", job.error)
            return

        # Poll for completion
        self._poll_run(job, run_id)
        if job.status in ("failed", "aborted"):
            return

        # Fetch results
        self._fetch_and_writeback(job, run_id)

    # --- generated_protocol mode ---

    def _execute_generated_protocol(self, job: Job) -> None:
        """Run a generated protocol from signed method/layout/analysis refs.

        For v1, this downloads the canonical JSON from eLabFTW using the
        refs in the job spec, then uses the analysis pipeline on the
        raw results. Protocol generation on the vm-agent uses the
        method spec to create a custom assay protocol.
        """
        method_ref = job.method_ref
        layout_ref = job.layout_ref
        analysis_ref = job.analysis_ref

        if not method_ref:
            job.status = "failed"
            job.error = "No method_ref for generated_protocol mode"
            job.add_event("execution_failed", job.error)
            return

        # Download canonical JSON specs from eLabFTW
        job.add_event("downloading_specs", "")
        try:
            method_spec = self._download_ref(method_ref)
            layout_spec = self._download_ref(layout_ref) if layout_ref else {}
            analysis_spec = self._download_ref(analysis_ref) if analysis_ref else {}
        except Exception as e:
            job.status = "failed"
            job.error = f"Failed to download specs: {e}"
            job.add_event("execution_failed", job.error)
            return

        job.add_event(
            "specs_downloaded",
            f"method={bool(method_spec)} layout={bool(layout_spec)} analysis={bool(analysis_spec)}",
        )

        if self.dry_run:
            job.status = "completed"
            job.add_event("dry_run_complete", "Specs validated, would run on instrument")
            return

        # For v1 generated_protocol, we need a protocol name to run on the instrument.
        # The method spec may contain a protocol_name field, or we use the
        # job's protocol_name field.
        protocol_name = job.protocol_name or method_spec.get("protocol_name", "")
        if not protocol_name:
            job.status = "failed"
            job.error = "No protocol_name in method spec or job for generated_protocol mode"
            job.add_event("execution_failed", job.error)
            return

        # Start the run
        job.add_event("starting_run", protocol_name)
        try:
            run_resp = self.vm_agent.start_run(protocol_name)
            run_id = run_resp.get("run_id", "")
            if not run_id:
                job.status = "failed"
                job.error = f"No run_id in response: {run_resp}"
                job.add_event("execution_failed", job.error)
                return
            job.run_id = run_id
            job.add_event("run_started", run_id)
        except VmAgentError as e:
            job.status = "failed"
            job.error = f"Failed to start run: {e}"
            job.add_event("execution_failed", job.error)
            return

        # Poll for completion
        self._poll_run(job, run_id)
        if job.status in ("failed", "aborted"):
            return

        # Fetch results and run analysis
        self._fetch_and_writeback(job, run_id, layout_spec, analysis_spec)

    # --- Shared helpers ---

    def _poll_run(self, job: Job, run_id: str) -> None:
        """Poll vm-agent for run completion. Updates job.status."""
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            if job.abort_requested:
                try:
                    self.vm_agent.abort_run(run_id)
                    job.add_event("abort_sent", run_id)
                except Exception as e:
                    job.add_event("abort_failed", str(e))
                job.status = "aborted"
                job.add_event("execution_aborted")
                return

            try:
                run = self.vm_agent.get_run(run_id)
            except VmAgentError as e:
                job.status = "failed"
                job.error = f"Failed to poll run status: {e}"
                job.add_event("execution_failed", job.error)
                return

            state = run.get("state", "").lower()
            is_error = run.get("is_error", False)

            if state in ("completed", "done", "finished"):
                job.add_event("run_completed", state)
                return

            if is_error or state in ("error", "failed"):
                job.status = "failed"
                job.error = f"Instrument run failed: state={state}"
                job.add_event("execution_failed", job.error)
                return

            time.sleep(POLL_INTERVAL)

        job.status = "failed"
        job.error = f"Run timed out after {POLL_TIMEOUT}s"
        job.add_event("execution_failed", job.error)

    def _fetch_and_writeback(
        self,
        job: Job,
        run_id: str,
        layout_spec: dict[str, Any] | None = None,
        analysis_spec_dict: dict[str, Any] | None = None,
    ) -> None:
        """Fetch results from vm-agent, run analysis, write back to eLabFTW."""
        job.add_event("fetching_results", run_id)
        try:
            results = self.vm_agent.get_run_results(run_id)
        except VmAgentError as e:
            job.status = "failed"
            job.error = f"Failed to fetch results: {e}"
            job.add_event("execution_failed", job.error)
            return

        raw_wells = results.get("wells", results.get("data", []))
        job.add_event("results_fetched", f"{len(raw_wells)} wells")

        # Run analysis if we have layout + analysis specs
        analyzed_csv = ""
        if layout_spec and analysis_spec_dict:
            try:
                layout_wells = {}
                for well in layout_spec.get("wells", []):
                    name = well.get("well_name", well.get("name", ""))
                    if name:
                        layout_wells[name] = well

                spec = AnalysisSpec.from_dict(analysis_spec_dict)
                analysis_result = self.analysis.run(raw_wells, layout_wells, spec)

                # Export analyzed results as CSV
                analyzed_csv = analysis_result.to_analyzed_wells_csv()
                job.add_event("analysis_complete", f"{len(analysis_result.wells)} wells analyzed")
            except Exception as e:
                job.add_event("analysis_failed", str(e))
                logger.warning("Analysis failed for job %s: %s", job.job_id, e)

        # Write back to eLabFTW
        self._writeback(job, raw_wells, analyzed_csv)

    def _writeback(self, job: Job, raw_wells: list[dict[str, Any]], analyzed_csv: str) -> None:
        """Write results back to eLabFTW as an experiment."""
        job.add_event("writeback_started", "")

        # Create or use existing experiment
        exp_id = job.elabftw_experiment_id
        try:
            if exp_id == 0:
                title = f"Wallac Victor2 — {job.title}"
                body = f"<p>Results from job <code>{job.job_id}</code></p>"
                exp_id = self.elabftw.create_experiment(title, body)
                job.elabftw_experiment_id = exp_id
                job.add_event("experiment_created", str(exp_id))

            # Upload raw results as JSON
            raw_json = json.dumps(raw_wells, indent=2, default=str)
            self.elabftw.upload_experiment_file(
                exp_id,
                f"{job.job_id}_raw_results.json",
                raw_json.encode(),
                comment="Raw per-well results from Wallac Victor2",
            )
            job.artifacts.append({"name": "raw_results.json", "type": "raw", "uploaded": True})
            job.add_event("raw_results_uploaded", "")

            # Upload analyzed results as CSV if available
            if analyzed_csv:
                self.elabftw.upload_experiment_file(
                    exp_id,
                    f"{job.job_id}_analyzed.csv",
                    analyzed_csv.encode(),
                    comment="Analyzed results from analysis pipeline",
                )
                job.artifacts.append({"name": "analyzed.csv", "type": "analyzed", "uploaded": True})
                job.add_event("analyzed_results_uploaded", "")

            # Patch experiment body with summary
            body = (
                f"<h2>Wallac Victor2 Results</h2>"
                f"<p><strong>Job ID:</strong> {job.job_id}</p>"
                f"<p><strong>Protocol:</strong> {job.protocol_name or 'N/A'}</p>"
                f"<p><strong>Run ID:</strong> {job.run_id}</p>"
                f"<p><strong>Wells measured:</strong> {len(raw_wells)}</p>"
                f"<p>Raw results and analyzed data are attached as files.</p>"
            )
            self.elabftw.patch_experiment(exp_id, {"body": body})

            job.status = "completed"
            job.add_event("writeback_completed", f"experiment={exp_id}")
            job.add_event("execution_completed", "")

        except Exception as e:
            job.status = "failed"
            job.error = f"Write-back failed: {e}"
            job.add_event("writeback_failed", str(e))
            logger.exception("Write-back failed for job %s", job.job_id)

    def _download_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Download a canonical JSON attachment from eLabFTW using a ref dict.

        Ref format: {"object_id": int, "hash": str, "attachment_id": int}
        """
        object_id = ref.get("object_id", 0)
        attachment_id = ref.get("attachment_id", 0)
        if not object_id or not attachment_id:
            return {}

        data = self.elabftw.download_upload(object_id, attachment_id)
        return json.loads(data)
