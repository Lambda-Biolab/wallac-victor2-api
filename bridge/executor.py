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


def _normalize_well_name(name: str) -> str:
    """Normalize a well address to non-zero-padded form: A01 → A1, H12 → H12."""
    name = name.strip().upper()
    if len(name) >= 2 and name[0].isalpha():
        row = name[0]
        try:
            col = int(name[1:])
            return f"{row}{col}"
        except ValueError:
            pass
    return name


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
            # If we got a job spec instead of a method spec (refs were mixed up),
            # follow the method.object_id reference to get the actual method spec
            if method_spec.get("schema_name") == "wallac.job":
                inner_method_ref = method_spec.get("method", {})
                if inner_method_ref.get("object_id") and inner_method_ref.get("json_attachment_id"):
                    job.add_event(
                        "following_method_ref", f"object_id={inner_method_ref['object_id']}"
                    )
                    method_spec = self._download_ref(inner_method_ref)
                else:
                    job.add_event(
                        "method_ref_incomplete", "method ref has no object_id or attachment_id"
                    )

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
        protocol_name, protocol_id = self._match_protocol_from_method(method_spec)
        if not protocol_name:
            job.status = "failed"
            job.error = "Could not match method spec to an instrument protocol"
            job.add_event("execution_failed", job.error)
            return
        job.add_event("protocol_matched", f"{protocol_name} (id={protocol_id})")

        # Clone the matched protocol with a new ID and update its PlateMap.
        # The OEM software (MlrMgr) caches protocols in memory and doesn't
        # re-read the PlateMap from the MDB when a run starts. By cloning
        # with a new ID, the OEM software is forced to read it fresh.
        cloned_proto_id = 0  # for cleanup (orphaned clone if PlateMap fails)
        run_proto_id = 0  # for start_run (only set if clone + PlateMap both succeed)
        if layout_spec:
            # Only include wells with role "measured" — skip "excluded" and "skipped"
            all_layout_wells = layout_spec.get("wells", [])
            wells = [
                w.get("well_name", w.get("name", ""))
                for w in all_layout_wells
                if w.get("well_name", w.get("name", "")) and w.get("role", "measured") == "measured"
            ]
            role_counts: dict[str, int] = {}
            for w in all_layout_wells:
                r = w.get("role", "measured")
                role_counts[r] = role_counts.get(r, 0) + 1
            job.add_event(
                "layout_wells_analyzed",
                f"total={len(all_layout_wells)} measured={len(wells)} roles={role_counts}",
            )
            if wells:
                try:
                    # Use the protocol ID directly (avoids ambiguous name
                    # lookup when cloned protocols with the same name exist)
                    template_id = protocol_id
                    if template_id:
                        new_id = int(time.time()) % 100000 + 2001000
                        clone_name = f"ELAB-Run-{new_id}"
                        self.vm_agent.clone_protocol(template_id, new_id, clone_name)
                        # Track for cleanup even if PlateMap update fails below.
                        cloned_proto_id = new_id
                        self.vm_agent.update_plate_map(new_id, wells)
                        # Both succeeded — use the cloned protocol for the run.
                        run_proto_id = new_id
                        job.add_event("protocol_cloned", f"id={new_id} wells={len(wells)}")
                except Exception as e:
                    job.add_event("protocol_clone_failed", str(e))
                    logger.warning("Protocol clone failed for %s: %s", protocol_name, e)
                    # cloned_proto_id stays set if clone succeeded (for cleanup).
                    # run_proto_id stays 0 — fall back to original protocol.
        # Resolve and start the run — use cloned protocol ID only if the
        # clone AND PlateMap update both succeeded. Otherwise fall back to
        # the original protocol ID (never the name, which can be ambiguous).
        run_protocol = run_proto_id if run_proto_id else protocol_id
        try:
            job.add_event("starting_run", str(run_protocol))
            run_resp = self.vm_agent.start_run(run_protocol)
            run_id = run_resp.get("run_id", "")
            if not run_id:
                job.status = "failed"
                job.error = f"No run_id in response: {run_resp}"
                job.add_event("execution_failed", job.error)
                return
            job.run_id = run_id
            job.add_event("run_started", run_id)

            # Clear any stale live_wells from a previous run
            job.live_wells = []

            # Poll for completion
            self._poll_run(job, run_id)
            if job.status in ("failed", "aborted"):
                return

            # Fetch results and run analysis
            self._fetch_and_writeback(job, run_id, layout_spec, analysis_spec)
        finally:
            # Always clean up the cloned protocol, even on exception.
            self._cleanup_cloned_protocol(cloned_proto_id)

    # --- Shared helpers ---

    def _cleanup_cloned_protocol(self, proto_id: int) -> None:
        """Delete a cloned protocol after the run. Best-effort."""
        if not proto_id:
            return
        try:
            self.vm_agent.delete_protocol(proto_id)
            logger.info("Cleaned up cloned protocol %s", proto_id)
        except Exception as e:
            logger.warning("Failed to clean up cloned protocol %s: %s", proto_id, e)

    def _poll_run(self, job: Job, run_id: str) -> None:
        """Poll vm-agent for run completion. Updates job.status.

        Also fetches live results every few seconds so the Run Builder
        can display a real-time heatmap of wells as they are measured.

        Abort handling: the vm-agent rejects aborts for runs younger than
        60s (aborting earlier wedges the instrument). If abort is requested
        while the run is too young, we keep polling and retry once the
        60s threshold is reached. If the run completes before then, we
        accept the result.
        """
        deadline = time.monotonic() + POLL_TIMEOUT
        run_start = time.monotonic()
        last_live_fetch = 0.0
        abort_tried = False
        while time.monotonic() < deadline:
            if job.abort_requested and not abort_tried:
                try:
                    self.vm_agent.abort_run(run_id)
                    job.add_event("abort_sent", run_id)
                    abort_tried = True
                except VmAgentError as e:
                    if e.status_code == 425:
                        # Too early to abort — keep polling, retry next iteration.
                        # The 425 response includes how many seconds to wait.
                        pass
                    else:
                        job.add_event("abort_failed", str(e))
                        abort_tried = True
                except Exception as e:
                    job.add_event("abort_failed", str(e))
                    abort_tried = True

            try:
                run = self.vm_agent.get_run(run_id)
            except VmAgentError as e:
                job.status = "failed"
                job.error = f"Failed to poll run status: {e}"
                job.add_event("execution_failed", job.error)
                return

            state = run.get("state", "").lower()

            # Fetch live results every ~3s for real-time heatmap.
            # The vm-agent's live buffer may only contain recently measured
            # wells, so we accumulate across polls to build the full picture.
            # Skip the first few seconds while the instrument is still
            # initializing — the live buffer may contain stale data from
            # the previous run until the new run starts writing to it.
            now = time.monotonic()
            if now - last_live_fetch >= 3.0 and now - run_start > 5.0:
                last_live_fetch = now
                try:
                    live = self.vm_agent.get_run_results(run_id)
                    wells = live.get("wells", live.get("data", []))
                    if wells:
                        # Merge new wells into existing live_wells (new overwrites old)
                        existing = {w["well"]: w for w in job.live_wells}
                        for w in wells:
                            name = _normalize_well_name(w.get("well", w.get("well_name", "")))
                            # Skip non-well entries (BKG, empty, etc.)
                            if not name or name[0] not in "ABCDEFGH":
                                continue
                            existing[name] = {
                                "well": name,
                                "od": w.get("od"),
                                "counts": w.get("counts"),
                            }
                        job.live_wells = list(existing.values())
                except Exception:
                    pass  # live fetch is best-effort

            # vm-agent terminal states: "measured" (completed successfully),
            # "completed", "done", "finished"
            if state in ("measured", "completed", "done", "finished"):
                if job.abort_requested and abort_tried:
                    job.status = "aborted"
                    job.add_event("execution_aborted", "run stopped")
                else:
                    job.add_event("run_completed", state)
                return

            # Error states: "error", "failed", "aborted"
            if state in ("error", "failed", "aborted"):
                if job.abort_requested and abort_tried and state == "aborted":
                    job.status = "aborted"
                    job.add_event("execution_aborted")
                else:
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
        """Fetch results from vm-agent, run analysis, write back to eLabFTW.

        Retries fetching results because the OEM app doesn't flush them to
        the MDB immediately after IsMeasured flips.
        """
        job.add_event("fetching_results", run_id)
        raw_wells: list[dict[str, Any]] = []
        for _ in range(8):
            try:
                results = self.vm_agent.get_run_results(run_id)
            except VmAgentError as e:
                job.status = "failed"
                job.error = f"Failed to fetch results: {e}"
                job.add_event("execution_failed", job.error)
                return
            raw_wells = results.get("wells", results.get("data", []))
            if raw_wells:
                break
            time.sleep(2.0)  # OEM app hasn't flushed yet

        job.add_event("results_fetched", f"{len(raw_wells)} wells")

        # Normalize well names (A01 → A1) so they're consistent across
        # the heatmap, analysis pipeline, and raw JSON attachment.
        for w in raw_wells:
            wn = w.get("well", "")
            if wn:
                w["well"] = _normalize_well_name(wn)

        # Filter to only the measured wells from the layout spec.
        # The vm-agent returns all 96 persisted rows even when only a
        # subset was measured (the rest have near-zero placeholder values).
        if layout_spec:
            measured_names = {
                _normalize_well_name(w.get("well_name", w.get("name", "")))
                for w in layout_spec.get("wells", [])
                if w.get("role", "measured") == "measured"
            }
            raw_wells = [w for w in raw_wells if w.get("well", "") in measured_names]
            job.add_event("results_filtered", f"{len(raw_wells)} measured wells")

        # Run analysis if we have layout + analysis specs
        analyzed_csv = ""
        if layout_spec and analysis_spec_dict:
            try:
                layout_wells = {}
                for well in layout_spec.get("wells", []):
                    name = well.get("well_name", well.get("name", ""))
                    if name:
                        layout_wells[_normalize_well_name(name)] = well

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

        # Extract well values into a dict keyed by normalized well name.
        # vm-agent returns zero-padded names (A01, A12); heatmap uses A1, A12.
        well_values: dict[str, float] = {}
        for w in raw_wells:
            name = w.get("well_name", w.get("name", w.get("well", "")))
            if not name:
                continue
            name = _normalize_well_name(name)
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

    def _match_protocol_from_method(self, method_spec: dict[str, Any]) -> tuple[str, int]:
        """Match a method spec to a protocol on the instrument.

        Returns (protocol_name, protocol_id), or ("", 0) if no match found.
        Prefers factory presets over custom protocols to avoid matching
        leftover clones from previous runs.
        """
        mode = method_spec.get("mode", "")

        # Get the list of all protocols from the vm-agent, forcing a cache
        # refresh so deleted clones from previous runs don't appear.
        try:
            prots_resp = self.vm_agent.get_protocols(refresh=True)
            prots = (
                prots_resp.get("protocols", prots_resp)
                if isinstance(prots_resp, dict)
                else prots_resp
            )
            all_protocols = prots
        except Exception:
            return "", 0

        if mode == "photometry":
            photo = method_spec.get("photometry", {})
            filter_id = photo.get("filter_id", "")
            read_time = photo.get("read_time_seconds", 1.0)

            # Map filter_id to the wavelength used in the protocol name.
            # P610 is the physical 610nm bandpass filter, used for OD600.
            # The instrument has a custom protocol "Absorbance @ 600 (1.0s)"
            # that uses this filter (MeasSequence L:2200000).
            filter_to_wavelength = {
                "P610": "600",  # OD600 — 610nm filter, protocol named @ 600
                "P405": "405",
                "P450": "450",
                "P490": "490",
                "P260": "260",
                "P280": "280",
            }
            wavelength = filter_to_wavelength.get(filter_id, "")
            if not wavelength:
                logger.warning("Unknown photometry filter_id: %s", filter_id)
                return "", 0

            # Build the expected protocol name: "Absorbance @ {wl} ({time}s)"
            # The instrument uses 1.0s or 0.1s — format to match exactly
            time_str = f"{read_time:.1f}"
            target = f"Absorbance @ {wavelength} ({time_str}s)"

            # Try exact match first, preferring factory presets
            factory = [p for p in all_protocols if p.get("factory_preset")]
            custom = [p for p in all_protocols if not p.get("factory_preset")]
            for pool in (factory, custom):
                for p in pool:
                    if p["name"] == target:
                        return p["name"], p["id"]

            # Fallback: match by wavelength only (ignore read time)
            for pool in (factory, custom):
                for p in pool:
                    if "Absorbance" in p["name"] and f"@ {wavelength}" in p["name"]:
                        return p["name"], p["id"]

            logger.warning(
                "No photometry protocol found for filter_id=%s wavelength=%s time=%s",
                filter_id,
                wavelength,
                time_str,
            )

        elif mode == "fluorometry":
            fluoro = method_spec.get("fluorometry", {})
            ex_filter_id = fluoro.get("excitation_filter_id", "")
            em_filter_id = fluoro.get("emission_filter_id", "")
            read_time = fluoro.get("read_time_seconds", 1.0)

            # Map filter_id pairs to protocol names.
            # The instrument has Fluorescein (485/535) and Umbelliferone (355/460).
            filter_pair_to_protocol = {
                ("F485", "F535"): "Fluorescein",
                ("F355", "F460"): "Umbelliferone",
            }
            dye_name = filter_pair_to_protocol.get((ex_filter_id, em_filter_id), "")
            if not dye_name:
                logger.warning(
                    "Unknown fluorometry filter pair: ex=%s em=%s",
                    ex_filter_id,
                    em_filter_id,
                )
                return "", 0

            # Build expected protocol name: "{dye} ({ex}nm/{em}nm, {time}s)"
            # Extract wavelengths from filter IDs (F485 → 485, F355 → 355)
            ex_wl = ex_filter_id[1:] if ex_filter_id.startswith("F") else ""
            em_wl = em_filter_id[1:] if em_filter_id.startswith("F") else ""
            time_str = f"{read_time:.1f}"
            target = f"{dye_name} ({ex_wl}nm/{em_wl}nm, {time_str}s)"

            # Try exact match first, preferring factory presets
            factory = [p for p in all_protocols if p.get("factory_preset")]
            custom = [p for p in all_protocols if not p.get("factory_preset")]
            for pool in (factory, custom):
                for p in pool:
                    if p["name"] == target:
                        return p["name"], p["id"]

            # Fallback: match by dye name + wavelengths (ignore read time)
            for pool in (factory, custom):
                for p in pool:
                    if (
                        dye_name in p["name"]
                        and ex_wl in p["name"]
                        and em_wl in p["name"]
                        and "Bottom" not in p["name"]
                        and "High Count" not in p["name"]
                    ):
                        return p["name"], p["id"]

            # Last resort: any match with dye name + wavelengths
            for pool in (factory, custom):
                for p in pool:
                    if dye_name in p["name"] and ex_wl in p["name"] and em_wl in p["name"]:
                        return p["name"], p["id"]

            logger.warning(
                "No fluorometry protocol found for dye=%s ex=%s em=%s time=%s",
                dye_name,
                ex_wl,
                em_wl,
                time_str,
            )

        elif mode == "luminescence":
            # Single luminescence protocol
            factory = [p for p in all_protocols if p.get("factory_preset")]
            custom = [p for p in all_protocols if not p.get("factory_preset")]
            for pool in (factory, custom):
                for p in pool:
                    if p["name"] == "Luminescence":
                        return p["name"], p["id"]

        return "", 0
