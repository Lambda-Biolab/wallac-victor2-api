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

import contextlib
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

        # Match the method spec to a factory preset protocol on the instrument.
        # The method spec defines mode (photometry/fluorometry/luminescence)
        # and filter/wavelength parameters. We map these to the closest
        # factory preset protocol name.
        protocol_name = self._match_protocol_from_method(method_spec)
        if not protocol_name:
            job.status = "failed"
            job.error = "Could not match method spec to an instrument protocol"
            job.add_event("execution_failed", job.error)
            return
        job.add_event("protocol_matched", protocol_name)

        # Resolve and start the run
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

            # vm-agent terminal states: "measured" (completed successfully),
            # "completed", "done", "finished"
            if state in ("measured", "completed", "done", "finished"):
                job.add_event("run_completed", state)
                return

            # Error states: "error", "failed", "aborted"
            if state in ("error", "failed", "aborted"):
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
        """Write results back to eLabFTW as an experiment.

        The experiment body contains:
        - Job metadata (ID, protocol, run ID)
        - A 96-well plate heatmap with color-coded values
        - A results table with per-well readings
        - Raw JSON and analyzed CSV as downloadable attachments
        """
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

            # Build rich HTML body with plate heatmap + results table
            body = self._build_results_html(job, raw_wells)
            self.elabftw.patch_experiment(exp_id, {"body": body})

            job.status = "completed"
            job.add_event("writeback_completed", f"experiment={exp_id}")
            job.add_event("execution_completed", "")

        except Exception as e:
            job.status = "failed"
            job.error = f"Write-back failed: {e}"
            job.add_event("writeback_failed", str(e))
            logger.exception("Write-back failed for job %s", job.job_id)

    def _build_results_html(self, job: Job, raw_wells: list[dict[str, Any]]) -> str:
        """Build a rich HTML body with plate heatmap and results table."""
        import html as html_mod

        # Extract well values into a dict keyed by well name
        well_values: dict[str, float] = {}
        for w in raw_wells:
            name = w.get("well_name", w.get("name", w.get("well", "")))
            if not name:
                continue
            # Try common value field names
            val = (
                w.get("primary_value")
                or w.get("od")
                or w.get("value")
                or w.get("raw_value")
                or w.get("counts")
                or w.get("intensity")
            )
            if val is not None:
                with contextlib.suppress(ValueError, TypeError):
                    well_values[name] = float(val)

        # Compute min/max for color scaling
        vals = list(well_values.values())
        vmin = min(vals) if vals else 0.0
        vmax = max(vals) if vals else 1.0
        vrange = vmax - vmin if vmax > vmin else 1.0

        def color_for(val: float) -> str:
            """Map a value to a blue-white-red color scale."""
            if vrange == 0:
                return "#ffffff"
            t = (val - vmin) / vrange  # 0..1
            # Blue (low) → white (mid) → red (high)
            if t < 0.5:
                # Blue to white
                r = int(255 * (t * 2))
                g = int(255 * (t * 2))
                b = 255
            else:
                # White to red
                r = 255
                g = int(255 * (1 - (t - 0.5) * 2))
                b = int(255 * (1 - (t - 0.5) * 2))
            return f"#{r:02x}{g:02x}{b:02x}"

        # Build plate heatmap (8 rows x 12 cols)
        rows = "ABCDEFGH"
        cols = list(range(1, 13))

        plate_html = [
            '<table style="border-collapse:collapse; font-size:0.75rem;">',
            "<tr><td></td>",
        ]
        for c in cols:
            plate_html.append(
                f'<td style="text-align:center; padding:2px 6px; font-weight:bold;">{c}</td>'
            )
        plate_html.append("</tr>")

        for r in rows:
            plate_html.append(f'<tr><td style="font-weight:bold; padding:2px 6px;">{r}</td>')
            for c in cols:
                well = f"{r}{c}"
                val = well_values.get(well)
                if val is not None:
                    color = color_for(val)
                    plate_html.append(
                        f'<td style="background:{color}; text-align:center; padding:2px 6px;'
                        f' border:1px solid #ccc;">{val:.3f}</td>'
                    )
                else:
                    plate_html.append(
                        '<td style="background:#f0f0f0; text-align:center; padding:2px 6px;'
                        ' border:1px solid #ccc; color:#999;">—</td>'
                    )
            plate_html.append("</tr>")
        plate_html.append("</table>")

        # Build results table (top 20 wells by value, or all if fewer)
        sorted_wells = sorted(well_values.items(), key=lambda x: x[1], reverse=True)
        table_rows = sorted_wells[:20]

        table_html = [
            '<table style="border-collapse:collapse; font-size:0.85rem; margin-top:12px;">',
            '<tr style="background:#e0e0e0;">',
            "<th style='padding:4px 12px; text-align:left; border:1px solid #ccc;'>Well</th>",
            "<th style='padding:4px 12px; text-align:right; border:1px solid #ccc;'>Value</th>",
            "</tr>",
        ]
        for well, val in table_rows:
            table_html.append(
                f"<tr>"
                f"<td style='padding:4px 12px; border:1px solid #ddd;'>{html_mod.escape(well)}</td>"
                f"<td style='padding:4px 12px; border:1px solid #ddd; text-align:right;'>{val:.4f}</td>"
                f"</tr>"
            )
        table_html.append("</table>")
        if len(sorted_wells) > 20:
            table_html.append(
                f"<p style='font-size:0.8rem; color:#666;'>Showing top 20 of {len(sorted_wells)} wells. "
                "See attached CSV for full results.</p>"
            )

        # Summary stats
        n_measured = len(well_values)
        mean_val = sum(vals) / len(vals) if vals else 0
        min_val = vmin if vals else 0
        max_val = vmax if vals else 0

        body = (
            f"<h2>Wallac Victor2 Results</h2>"
            f"<table style='border-collapse:collapse; margin-bottom:16px;'>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Job ID:</td><td>{html_mod.escape(job.job_id)}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Protocol:</td><td>{html_mod.escape(job.protocol_name or 'N/A')}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Run ID:</td><td>{html_mod.escape(job.run_id)}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Wells measured:</td><td>{n_measured}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Min / Mean / Max:</td><td>{min_val:.4f} / {mean_val:.4f} / {max_val:.4f}</td></tr>"
            f"</table>"
            f"<h3>Plate Heatmap</h3>"
            f"{''.join(plate_html)}"
            f"<h3>Results (Top Wells)</h3>"
            f"{''.join(table_html)}"
            f"<p style='margin-top:16px; font-size:0.85rem; color:#666;'>"
            f"Raw results (JSON) and analyzed data (CSV) are attached as files below."
            f"</p>"
        )
        return body

    def _download_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Download a canonical JSON attachment from eLabFTW using a ref dict.

        Ref format: {"object_id": int, "hash": str, "attachment_id": int}
        """
        object_id = ref.get("object_id", 0)
        attachment_id = ref.get("attachment_id", 0)
        if not object_id or not attachment_id:
            logger.warning("download_ref: missing object_id or attachment_id in ref: %s", ref)
            return {}

        try:
            data = self.elabftw.download_upload(object_id, attachment_id)
            return json.loads(data)
        except Exception as e:
            logger.warning(
                "download_ref failed for object=%s attachment=%s: %s", object_id, attachment_id, e
            )
            return {}

    def _match_protocol_from_method(self, method_spec: dict[str, Any]) -> str:
        """Match a method spec to a factory preset protocol name on the instrument.

        The method spec has a "mode" field (photometry/fluorometry/luminescence)
        and mode-specific settings with filter/wavelength and read time info.
        We map these to the closest factory preset protocol.

        Returns the protocol name, or "" if no match found.
        """
        mode = method_spec.get("mode", "")

        # Get the list of factory presets from the vm-agent
        try:
            prots_resp = self.vm_agent.get_protocols()
            prots = (
                prots_resp.get("protocols", prots_resp)
                if isinstance(prots_resp, dict)
                else prots_resp
            )
            factory_presets = [p for p in prots if p.get("factory_preset")]
        except Exception:
            return ""

        if mode == "photometry":
            photo = method_spec.get("photometry", {})
            filter_name = photo.get("filter_name", "")
            read_time = photo.get("read_time_seconds", 1.0)

            # Extract wavelength from filter name (e.g. "OD600" → 600, "405nm" → 405)
            import re

            wl_match = re.search(r"(\d+)", filter_name)
            wavelength = wl_match.group(1) if wl_match else ""

            # Match: "Absorbance @ {wl} ({time}s)"
            time_str = f"{read_time:.1f}" if read_time == int(read_time) else f"{read_time}"
            target = f"Absorbance @ {wavelength} ({time_str}s)"

            for p in factory_presets:
                if p["name"] == target:
                    return p["name"]
            # Fallback: match by wavelength only
            for p in factory_presets:
                if "Absorbance" in p["name"] and f"@ {wavelength}" in p["name"]:
                    return p["name"]

        elif mode == "fluorometry":
            fluoro = method_spec.get("fluorometry", {})
            ex_name = fluoro.get("excitation_filter_name", "")
            em_name = fluoro.get("emission_filter_name", "")
            read_time = fluoro.get("read_time_seconds", 1.0)

            # Extract wavelengths from filter names
            import re

            ex_match = re.search(r"(\d+)", ex_name)
            em_match = re.search(r"(\d+)", em_name)
            ex_wl = ex_match.group(1) if ex_match else ""
            em_wl = em_match.group(1) if em_match else ""

            time_str = f"{read_time:.1f}" if read_time == int(read_time) else f"{read_time}"
            target = f"({ex_wl}nm/{em_wl}nm, {time_str}s)"

            for p in factory_presets:
                if target in p["name"]:
                    return p["name"]
            # Fallback: match by filter names
            for p in factory_presets:
                if ex_wl and em_wl and ex_wl in p["name"] and em_wl in p["name"]:
                    return p["name"]

        elif mode == "luminescence":
            # Single luminescence protocol
            for p in factory_presets:
                if p["name"] == "Luminescence":
                    return p["name"]

        return ""
