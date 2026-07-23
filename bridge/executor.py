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

import html
import json
import logging
import time
from typing import Any, Callable

from .analysis import AnalysisPipeline
from .canonical import verify_hash
from .elabftw import ElabftwClient
from .errors import SIGNATURE_MISSING, BridgeError, Severity
from .jobs import UNKNOWN, Job
from .schemas import AnalysisSpec, LayoutSpec, MethodSpec
from .vm_agent_client import VmAgentClient, VmAgentError
from .well_utils import normalize_well_name as _normalize_well_name

logger = logging.getLogger(__name__)

# Poll interval for vm-agent run status (seconds)
POLL_INTERVAL = 1.0
# Maximum time to wait for a run to complete (seconds)
POLL_TIMEOUT = 600.0


def _assay_matches(
    entry: dict[str, Any],
    proto_name: str,
    proto_id: int,
) -> bool:
    """Return True when a MDB entry matches the requested protocol identity."""
    entry_proto_id = entry.get("protocol_id")
    if proto_id and entry_proto_id is not None:
        return str(entry_proto_id) == str(proto_id)
    if proto_name:
        return entry.get("protocol_name", "") == proto_name
    return not proto_id


def _find_assay_after(
    vm_agent,
    max_before: int,
    proto_name: str,
    proto_id: int = 0,
) -> int:
    """Find the newest MDB assay_id greater than max_before.

    Prefer protocol ID when supplied. For older vm-agent entries that omit
    protocol_id, fall back to exact protocol-name matching rather than risk
    selecting an unrelated concurrent assay.
    """
    try:
        jobs_list = vm_agent.get_jobs()
        jobs = jobs_list.get("jobs", jobs_list) if isinstance(jobs_list, dict) else jobs_list
        best_id = 0
        for entry in jobs:
            jid = entry.get("assay_id", 0)
            if jid <= max_before or jid <= best_id:
                continue
            if _assay_matches(entry, proto_name, proto_id):
                best_id = jid
        return best_id
    except Exception:
        return 0


def _well_values(raw_wells: list[dict[str, Any]]) -> dict[str, float]:
    """Extract the first available numeric reading for each normalized well."""
    values: dict[str, float] = {}
    value_fields = ("primary_value", "od", "value", "raw_value", "counts", "intensity")
    for reading in raw_wells:
        name = reading.get("well_name", reading.get("name", reading.get("well", "")))
        if not name:
            continue
        for field in value_fields:
            value = reading.get(field)
            if value is None:
                continue
            # Reason: zero is a valid reading; only absent or non-numeric
            # values should fall through to the next supported field.
            try:
                values[_normalize_well_name(name)] = float(value)
                break
            except (ValueError, TypeError):
                continue
    return values


def _heatmap_color(value: float, minimum: float, value_range: float) -> str:
    """Map a value to a blue-white-red heatmap color."""
    position = (value - minimum) / value_range
    if position < 0.5:
        red = green = int(255 * position * 2)
        blue = 255
    else:
        red = 255
        green = blue = int(255 * (1 - (position - 0.5) * 2))
    return f"#{red:02x}{green:02x}{blue:02x}"


def _plate_heatmap_html(
    values: dict[str, float],
    minimum: float,
    value_range: float,
) -> str:
    """Render an eight-by-twelve plate heatmap."""
    cells = [
        '<table style="border-collapse:collapse; font-size:0.75rem;">',
        "<tr><td></td>",
    ]
    for column in range(1, 13):
        cells.append(
            f'<td style="text-align:center; padding:2px 6px; font-weight:bold;">{column}</td>'
        )
    cells.append("</tr>")
    for row in "ABCDEFGH":
        cells.append(f'<tr><td style="font-weight:bold; padding:2px 6px;">{row}</td>')
        for column in range(1, 13):
            value = values.get(f"{row}{column}")
            if value is None:
                cells.append(
                    '<td style="background:#f0f0f0; text-align:center; padding:2px 6px;'
                    ' border:1px solid #ccc; color:#999;">—</td>'
                )
                continue
            color = _heatmap_color(value, minimum, value_range)
            cells.append(
                f'<td style="background:{color}; text-align:center; padding:2px 6px;'
                f' border:1px solid #ccc;">{value:.3f}</td>'
            )
        cells.append("</tr>")
    cells.append("</table>")
    return "".join(cells)


def _top_results_html(values: dict[str, float]) -> str:
    """Render the twenty highest well readings as a compact table."""
    sorted_wells = sorted(values.items(), key=lambda item: item[1], reverse=True)
    rows = [
        '<table style="border-collapse:collapse; font-size:0.85rem; margin-top:12px;">',
        '<tr style="background:#e0e0e0;">',
        "<th style='padding:4px 12px; text-align:left; border:1px solid #ccc;'>Well</th>",
        "<th style='padding:4px 12px; text-align:right; border:1px solid #ccc;'>Value</th>",
        "</tr>",
    ]
    for well, value in sorted_wells[:20]:
        rows.append(
            f"<tr><td style='padding:4px 12px; border:1px solid #ddd;'>{html.escape(well)}</td>"
            f"<td style='padding:4px 12px; border:1px solid #ddd; text-align:right;'>"
            f"{value:.4f}</td></tr>"
        )
    rows.append("</table>")
    if len(sorted_wells) > 20:
        rows.append(
            f"<p style='font-size:0.8rem; color:#666;'>Showing top 20 of {len(sorted_wells)} "
            "wells. See attached CSV for full results.</p>"
        )
    return "".join(rows)


def _ordered_protocols(protocols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return factory presets before custom protocols."""
    factory = [protocol for protocol in protocols if protocol.get("factory_preset")]
    custom = [protocol for protocol in protocols if not protocol.get("factory_preset")]
    return factory + custom


def _protocol_match(
    protocols: list[dict[str, Any]],
    predicate: Callable[[str], bool],
) -> tuple[str, int]:
    """Return the first ordered protocol accepted by a predicate."""
    for protocol in _ordered_protocols(protocols):
        if predicate(str(protocol.get("name", ""))):
            return str(protocol["name"]), int(protocol["id"])
    return "", 0


def _match_photometry(
    method_spec: dict[str, Any],
    protocols: list[dict[str, Any]],
) -> tuple[str, int]:
    """Match photometry settings to the closest installed protocol."""
    photometry = method_spec.get("photometry", {})
    filter_id = photometry.get("filter_id", "")
    wavelength = {
        "P610": "600",
        "P405": "405",
        "P450": "450",
        "P490": "490",
        "P260": "260",
        "P280": "280",
    }.get(filter_id, "")
    if not wavelength:
        logger.warning("Unknown photometry filter_id: %s", filter_id)
        return "", 0
    time_string = f"{photometry.get('read_time_seconds', 1.0):.1f}"
    target = f"Absorbance @ {wavelength} ({time_string}s)"
    match = _protocol_match(protocols, lambda name: name == target)
    if match[0]:
        return match
    match = _protocol_match(
        protocols,
        lambda name: "Absorbance" in name and f"@ {wavelength}" in name,
    )
    if not match[0]:
        logger.warning(
            "No photometry protocol found for filter_id=%s wavelength=%s time=%s",
            filter_id,
            wavelength,
            time_string,
        )
    return match


def _match_fluorometry(
    method_spec: dict[str, Any],
    protocols: list[dict[str, Any]],
) -> tuple[str, int]:
    """Match fluorometry settings to the closest installed protocol."""
    fluorometry = method_spec.get("fluorometry", {})
    excitation = fluorometry.get("excitation_filter_id", "")
    emission = fluorometry.get("emission_filter_id", "")
    dye = {
        ("F485", "F535"): "Fluorescein",
        ("F355", "F460"): "Umbelliferone",
    }.get((excitation, emission), "")
    if not dye:
        logger.warning("Unknown fluorometry filter pair: ex=%s em=%s", excitation, emission)
        return "", 0
    excitation_wavelength = excitation.removeprefix("F")
    emission_wavelength = emission.removeprefix("F")
    time_string = f"{fluorometry.get('read_time_seconds', 1.0):.1f}"
    target = f"{dye} ({excitation_wavelength}nm/{emission_wavelength}nm, {time_string}s)"
    match = _protocol_match(protocols, lambda name: name == target)
    if match[0]:
        return match
    identifiers = (dye, excitation_wavelength, emission_wavelength)
    match = _protocol_match(
        protocols,
        lambda name: (
            all(identifier in name for identifier in identifiers)
            and "Bottom" not in name
            and "High Count" not in name
        ),
    )
    if not match[0]:
        match = _protocol_match(
            protocols,
            lambda name: all(identifier in name for identifier in identifiers),
        )
    if not match[0]:
        logger.warning(
            "No fluorometry protocol found for dye=%s ex=%s em=%s time=%s",
            dye,
            excitation_wavelength,
            emission_wavelength,
            time_string,
        )
    return match


def _match_luminescence(
    _method_spec: dict[str, Any],
    protocols: list[dict[str, Any]],
) -> tuple[str, int]:
    """Match the single supported luminescence protocol."""
    return _protocol_match(protocols, lambda name: name == "Luminescence")


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

    @staticmethod
    def _fail_job(job: Job, message: str) -> None:
        """Move a job to failed and record the public failure event."""
        job.status = "failed"
        job.error = message
        job.add_event("execution_failed", message)

    @staticmethod
    def _mark_operator_review(job: Job, message: str, detail: str = "") -> None:
        """Move a job to unknown_requires_operator_review.

        Used per docs/abort-recovery.md when the run reached a
        measured/completed state but the bridge cannot trust the output:
        requested analysis raised (raw results were still written back), or
        a measured run returned no well records at all (no explicit zero-well
        contract exists, so this is ambiguous rather than a clean completion).
        A valid zero-valued reading produces a well record with value 0.0 and
        stays ``completed`` — that case is distinct from "no wells returned".
        """
        job.status = UNKNOWN
        job.error = message
        job.add_event("operator_review_required", detail or message)

    def _find_protocol_by_name(self, protocol_name: str) -> dict[str, Any] | None:
        """Find a protocol by exact name in the vm-agent listing."""
        response = self.vm_agent.get_protocols()
        protocols = response.get("protocols", response) if isinstance(response, dict) else response
        return next(
            (protocol for protocol in protocols if protocol.get("name") == protocol_name), None
        )

    def _resolve_existing_protocol(self, job: Job) -> dict[str, Any] | None:
        """Resolve an existing protocol by ID, direct name, then list fallback."""
        if job.protocol_id:
            job.add_event("protocol_resolved", f"id={job.protocol_id} (direct)")
            return {
                "id": job.protocol_id,
                "name": job.protocol_name or f"protocol_{job.protocol_id}",
            }

        job.add_event("resolving_protocol", job.protocol_name)
        try:
            protocol = self.vm_agent.get_protocol(job.protocol_name)
            job.add_event("protocol_resolved", f"id={protocol.get('id')}")
            return protocol
        except VmAgentError as error:
            if error.status_code != 404:
                self._fail_job(job, f"Protocol '{job.protocol_name}' not found: {error}")
                return None

        try:
            protocol = self._find_protocol_by_name(job.protocol_name)
        except VmAgentError as error:
            self._fail_job(job, f"Protocol '{job.protocol_name}' not found: {error}")
            return None
        if protocol is None:
            self._fail_job(job, f"Protocol '{job.protocol_name}' not found by name or ID")
            return None
        job.add_event("protocol_resolved", f"id={protocol.get('id')} (via list search)")
        return protocol

    def _snapshot_max_assay_id(self, job: Job) -> bool:
        """Capture the latest MDB assay ID before a run can safely start."""
        try:
            response = self.vm_agent.get_jobs()
            jobs = response.get("jobs", response) if isinstance(response, dict) else response
            maximum = max((entry.get("assay_id", 0) for entry in jobs), default=0)
        except Exception as error:
            job.add_event("assay_snapshot_failed", str(error))
            logger.warning("Failed to snapshot pre-run assay id for job %s", job.job_id)
            return False
        job.max_assay_before = maximum
        job.add_event("assay_snapshot_before", str(maximum))
        return True

    def _start_job_run(self, job: Job, protocol: str | int) -> str:
        """Start a vm-agent run and return its ID, failing the job on error."""
        # Reason: request_abort() takes the same lock. Whichever operation
        # acquires it first determines whether physical work starts: an abort
        # first skips start_run; a start first publishes run_id before abort
        # is accepted so polling can abort that concrete run.
        with job._run_start_lock:
            if job.abort_requested:
                job.status = "aborted"
                job.add_event("execution_aborted", "aborted before run start")
                return ""
            job.add_event("starting_run", str(protocol))
            try:
                response = self.vm_agent.start_run(protocol)
            except VmAgentError as error:
                self._fail_job(job, f"Failed to start run: {error}")
                return ""
            run_id = response.get("run_id", "")
            if not run_id:
                self._fail_job(job, f"No run_id in response: {response}")
                return ""
            job.run_id = run_id
            job.add_event("run_started", run_id)
            return run_id

    # --- existing_protocol mode ---

    def _execute_existing_protocol(self, job: Job) -> None:
        """Run a factory preset protocol by name or ID.

        With no ``wells_spec`` override the instrument runs the resolved
        protocol against its factory 96-well plate map. When the caller
        supplies a non-empty ``wells_spec`` (any of ``{"wells": [...]}``,
        ``{"rows": [...]}``, ``{"all": true}``), the executor clones the
        protocol into a per-run ID, applies the override via
        ``PATCH /mdb/protocols/{id}/wells`` on the clone, runs against the
        clone, and deletes the clone in ``finally`` so the factory preset
        is never touched. Cloning is required because the OEM stack caches
        protocols in memory and does not re-read the PlateMap from the MDB —
        the only way to force a fresh read of the new plate map is to give
        the run a new protocol id.
        """
        if not job.protocol_name and not job.protocol_id:
            self._fail_job(
                job,
                "No protocol_name or protocol_id specified for existing_protocol mode",
            )
            return
        protocol = self._resolve_existing_protocol(job)
        if protocol is None:
            return

        if self.dry_run:
            # Reason: dry-run is a no-op for plate-map override too. The
            # factory plate map is the run the bridge *would* have executed;
            # we surface the requested wells in the event so operators can
            # audit what would happen.
            wells_summary = self._wells_spec_summary(job)
            job.status = "completed"
            job.add_event(
                "dry_run_complete",
                f"Would run protocol {job.protocol_name}{wells_summary}",
            )
            return

        # Plate-map override path: clone the factory preset, PATCH the plate
        # map on the clone, run on the clone, and clean up the clone in
        # ``finally``. The factory preset is never written to.
        cloned_proto_id = 0
        run_protocol = protocol.get("id", job.protocol_name)
        wells_to_measure = self._extract_wells_from_spec(job.wells_spec)
        if wells_to_measure is not None:
            try:
                run_protocol, cloned_proto_id = self._clone_with_wells(
                    job, protocol, wells_to_measure
                )
            except Exception as error:
                self._fail_job(
                    job,
                    f"Protocol clone or plate-map apply failed: {error}",
                )
                return
            job.protocol_name = protocol.get("name", job.protocol_name)
            job.protocol_id = run_protocol
            # Reason: MDB assay_id resolution filters by protocol_id, so
            # the clone id is authoritative for the post-run result lookup.
            # If the snapshot fails we must clean up the clone before
            # returning; otherwise the instrument is left with a stub
            # protocol that floats in the MDB until manual cleanup.
            if not self._snapshot_max_assay_id(job):
                self._fail_job(job, "Could not safely snapshot assays before starting run")
                self._cleanup_cloned_protocol(cloned_proto_id)
                return
            try:
                self._run_to_completion(job, run_protocol, cloned_proto_id)
            finally:
                self._cleanup_cloned_protocol(cloned_proto_id)
            return

        # No override: use the factory plate map as-is.
        if not self._snapshot_max_assay_id(job):
            self._fail_job(job, "Could not safely snapshot assays before starting run")
            return
        self._run_to_completion(job, run_protocol, cloned_proto_id)

    def _run_to_completion(self, job: Job, run_protocol: int | str, cloned_proto_id: int) -> None:
        """Start the run, poll, and write back. No clone cleanup here —
        callers using the plate-map-override path own the cleanup via the
        ``finally`` block in :meth:`_execute_existing_protocol`."""
        run_id = self._start_job_run(job, run_protocol)
        if not run_id:
            return
        self._poll_run(job, run_id)
        if job.status in ("failed", "aborted"):
            return
        self._fetch_and_writeback(job, run_id)

    def _extract_wells_from_spec(self, wells_spec: dict[str, Any]) -> list[str] | None:
        """Normalize ``wells_spec`` to a list of well names, or None if empty.

        Returns ``None`` when the spec is empty or absent (caller should
        run the factory plate map). Returns a (possibly empty) list of
        explicit well names when the spec is populated. The vm-agent's
        ``/wells`` endpoint handles both ``{"wells": [...]}`` and
        ``{"rows": [...]}`` shapes natively; this method just normalizes
        what the bridge has stored on the job to that input format.
        """
        if not wells_spec:
            return None
        # Reason: an empty list inside the spec is a valid no-op (run the
        # factory plate map). Treat it the same as an absent spec so the
        # caller does not have to know that {"wells": []} is equivalent
        # to omitting the field.
        wells = wells_spec.get("wells") or []
        if not wells and not wells_spec.get("rows") and not wells_spec.get("all"):
            return None
        return [str(w).upper() for w in wells]

    def _clone_with_wells(
        self,
        job: Job,
        protocol: dict[str, Any],
        wells: list[str],
    ) -> tuple[int, int]:
        """Clone a factory preset and apply the plate-map override on the clone.

        Returns ``(run_protocol_id, cloned_proto_id)``. Raises on any
        clone or PATCH failure so the caller can fail the job without
        leaking a stub protocol on the instrument. The factory preset
        is never written to.

        The cloned protocol is named ``ELAB-Run-<new_id>`` so operators
        can audit any stub that survives a crash (e.g. crash between
        the clone and the ``finally`` cleanup) by name.
        """
        template_id = int(protocol.get("id", 0))
        if not template_id:
            raise ValueError(f"Protocol {protocol.get('name', '?')!r} has no id to clone from")
        # Reason: pick a fresh id in the ELAB-Run- namespace (the same
        # window used by the existing generated-protocol clone helper).
        # Modulo 100000 plus 2001000 means a run every second for ~27
        # hours before id collision risk; the MDB write lock serializes
        # simultaneous creates.
        new_id = int(time.time()) % 100000 + 2001000
        try:
            self.vm_agent.clone_protocol(template_id, new_id, f"ELAB-Run-{new_id}")
        except Exception as error:
            job.add_event("protocol_clone_failed", str(error))
            logger.warning(
                "Protocol clone failed for %s (template=%s): %s",
                job.protocol_name,
                template_id,
                error,
            )
            raise
        # Reason: clone succeeded → partial protocol exists on the
        # instrument. If the PATCH then fails we must clean up the
        # partial clone before re-raising, otherwise the instrument is
        # left with a stub protocol that floats in the MDB until manual
        # cleanup and may shadow the factory protocol on the next
        # assay lookup.
        try:
            self.vm_agent.set_protocol_wells(new_id, wells)
        except Exception as error:
            job.add_event("plate_map_apply_failed", str(error))
            logger.warning(
                "Plate map apply failed for clone %s of %s: %s",
                new_id,
                job.protocol_name,
                error,
            )
            self._cleanup_cloned_protocol(new_id)
            raise
        job.add_event("protocol_cloned", f"id={new_id} wells={len(wells)}")
        return new_id, new_id

    @staticmethod
    def _wells_spec_summary(job: Job) -> str:
        """Compact human-readable summary of the requested wells for events."""
        spec = job.wells_spec or {}
        if not spec:
            return ""
        if spec.get("all"):
            return " (all 96 wells)"
        if spec.get("rows"):
            rows = ",".join(str(r) for r in spec["rows"])
            return f" (rows {rows})"
        wells = spec.get("wells") or []
        if wells:
            return f" (wells: {','.join(str(w) for w in wells)})"
        return ""

    def _load_generated_specs(
        self, job: Job
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        """Download and hash-check the canonical refs for a generated run.

        The ``generated_protocol`` schema contract (ExecutionMode.GENERATED_PROTOCOL
        in schemas.py + docs/plans/wallac-protocol-authoring.md "Validated
        workflow") requires a signed method.json, layout.json, *and*
        analysis.json. A missing ref means the submission does not carry the
        three signed objects the executor needs to interpret the run, so the
        job fails deterministically *before* any download attempt, dry-run
        success, or hardware start — preserving the no-physical-work
        guarantee for an incomplete submission.
        """
        missing = [
            name
            for name, ref in (
                ("method_ref", job.method_ref),
                ("layout_ref", job.layout_ref),
                ("analysis_ref", job.analysis_ref),
            )
            if not ref
        ]
        if missing:
            # Reason: short-circuit layout_ref/analysis_ref when unset (as the
            # old code did) silently produced an "empty spec" that passed
            # validation only because the validator skipped empty dicts,
            # then proceeded to dry_run_complete / hardware start without
            # the signed layout or analysis the schema contract mandates.
            detail = ", ".join(missing)
            message = f"Missing required ref(s) for generated_protocol mode: {detail}"
            self._fail_job(job, message)
            job.add_event("missing_required_ref", detail)
            return None
        job.add_event("downloading_specs", "")
        try:
            method_spec = self._download_ref(job.method_ref)
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
            layout_spec = self._download_ref(job.layout_ref)
            analysis_spec = self._download_ref(job.analysis_ref)
        except Exception as error:
            self._fail_job(job, f"Failed to download specs: {error}")
            return None
        job.add_event(
            "specs_downloaded",
            f"method={bool(method_spec)} layout={bool(layout_spec)} analysis={bool(analysis_spec)}",
        )
        return method_spec, layout_spec, analysis_spec

    @staticmethod
    def _validate_generated_specs(
        job: Job,
        method_spec: dict[str, Any],
        layout_spec: dict[str, Any],
        analysis_spec: dict[str, Any],
    ) -> bool:
        """Fail closed through schema/version/role/well validation.

        :meth:`_download_ref` only verifies the attachment hash matches the
        submitted reference. It does not enforce that the parsed JSON conforms
        to a supported :class:`MethodSpec`/``LayoutSpec``/``AnalysisSpec``
        schema. A referenced object can hash-match its bytes and still carry
        an unsupported schema version (e.g. a future ``wallac.method.v2``),
        an invalid well ``role``, or an out-of-range well name. Without this
        gate the executor would either silently drop layout wells with
        unknown roles or, in dry-run, report success for a spec it cannot
        interpret.

        Run before dry-run success and any hardware start so the executor
        never launches a run from an unintelligible spec. Reuses the
        :class:`BridgeError` (``SCHEMA_UNSUPPORTED``) raised by
        :func:`validate_schema_identity`, the ``ValueError`` raised by
        :class:`WellSpec.from_dict`, and the ``KeyError``/``TypeError``
        raised by dataclass ``from_dict`` direct-key indexing / ``float``
        coercion of malformed-but-hash-valid JSON, propagating them through
        the existing fail-closed (:meth:`_fail_job` + event log) pattern.

        The canonical dicts are preserved for the downstream matching/layout
        /analysis path, which reads them directly; this step is purely a
        fail-closed guard.
        """
        validators: tuple[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], Any]], ...] = (
            ("method", "wallac.method", method_spec, MethodSpec.from_dict),
            ("layout", "wallac.layout", layout_spec, LayoutSpec.from_dict),
            ("analysis", "wallac.analysis", analysis_spec, AnalysisSpec.from_dict),
        )
        for kind, expected_schema, spec, validator in validators:
            # Reason: an empty dict here must fail closed, not be skipped.
            # :meth:`_load_generated_specs` fails upstream when a ref is
            # missing, but the downloaded attachment can still decode to
            # an empty JSON object (``{}``) whose hash matches the signed
            # reference. Skipping it would mask an empty-but-signed spec
            # and let the executor proceed to dry_run_complete / hardware
            # start on a spec it cannot interpret (no schema_name, no
            # mode, no wells). The validator raises BridgeError for an
            # empty ``schema_name`` (``validate_schema_identity("", 0)``),
            # which the existing fail-closed path converts into the
            # ``spec_validation_failed`` event + ``failed`` status.
            # Reason: a hash-valid but wrong-kind ref (e.g. analysis_ref
            # pointing at a wallac.method object) still satisfies
            # ``validate_schema_identity`` for *some* supported schema, so
            # without this guard the parser would silently default
            # missing/optional fields and the wrong ref could flow into
            # the matching/layout/analysis path. Each generated ref must
            # carry its own schema kind exactly.
            if str(spec.get("schema_name", "")) != expected_schema:
                message = (
                    f"Spec validation failed for {kind} spec: expected "
                    f"schema_name '{expected_schema}', got "
                    f"{spec.get('schema_name', '')!r}"
                )
                BridgeExecutor._fail_job(job, message)
                job.add_event(
                    "spec_validation_failed",
                    f"kind={kind} code=schema_mismatch "
                    f"expected={expected_schema} "
                    f"actual={spec.get('schema_name', '')!r}",
                )
                return False
            try:
                validator(spec)
            except BridgeError as error:
                BridgeExecutor._fail_job(
                    job,
                    f"Spec validation failed for {kind} spec: {error.human_message}",
                )
                job.add_event(
                    "spec_validation_failed",
                    f"kind={kind} code={error.code} {error.human_message}",
                )
                return False
            except (ValueError, KeyError, TypeError) as error:
                # Reason: ``validate_schema_identity`` raises ``BridgeError``
                # (handled above) for unknown schema names/versions, but the
                # dataclass ``from_dict`` parsers use direct key indexing
                # (``d["mode"]``, ``d["wells"]``, ``d["photometry"]``) and
                # ``float(...)`` coercion. A hash-valid attachment can still
                # be missing a required field (``KeyError``) or carry a
                # wrong-typed value (``TypeError`` on ``float([])`` or
                # ``int(None)``). Normalize those parse failures into the
                # existing fail-closed + ``spec_validation_failed`` path so
                # they never reach dry-run success or a hardware start.
                BridgeExecutor._fail_job(
                    job,
                    f"Spec validation failed for {kind} spec: {error}",
                )
                job.add_event(
                    "spec_validation_failed",
                    f"kind={kind} {type(error).__name__}: {error}",
                )
                return False
        job.add_event("specs_validated", "method/layout/analysis schemas validated")
        return True

    @staticmethod
    def _measured_layout_wells(job: Job, layout_spec: dict[str, Any]) -> tuple[list[str], set[str]]:
        """Return physically acquired layout wells in raw and normalized forms."""
        layout_wells = layout_spec.get("wells", [])
        measured = [
            well.get("well_name", well.get("name", ""))
            for well in layout_wells
            if well.get("well_name", well.get("name", ""))
            and well.get("role", "measured") in ("measured", "excluded")
        ]
        role_counts: dict[str, int] = {}
        for well in layout_wells:
            role = well.get("role", "measured")
            role_counts[role] = role_counts.get(role, 0) + 1
        job.add_event(
            "layout_wells_analyzed",
            f"total={len(layout_wells)} acquired={len(measured)} roles={role_counts}",
        )
        return measured, {_normalize_well_name(name) for name in measured}

    def _clone_for_layout(
        self,
        job: Job,
        protocol_name: str,
        protocol_id: int,
        wells: list[str],
    ) -> tuple[int, int]:
        """Clone a protocol for a plate map, returning run and cleanup IDs.

        Fail closed — never fall back to the factory ``protocol_id``. A
        generated run needs the per-plate clone so the instrument's MDB
        PlateMap covers exactly the measured/excluded wells from the signed
        layout. Running against the factory preset instead would acquire
        the wrong wells (or all 96), reading from physical hardware the
        operator did not authorize for that layout. So a clone failure or a
        plate-map apply failure must abort the job, not silently execute
        against the factory protocol.

        On clone failure no partial clone exists on the instrument, so the
        caller only needs to mark the job failed. On plate-map apply failure
        a partial clone *does* exist (the clone succeeded but its plate map
        was never written); this method best-effort cleans that up before
        re-raising so the instrument is not left with a stub protocol.

        Raises:
            Exception: re-raises the underlying vm-agent error after logging
                the public event, so the caller can fail the job from a
                single place (:meth:`_execute_generated_protocol`).
        """
        # Reason: never return the factory ``protocol_id`` for a missing
        # ``protocol_id`` or an empty ``wells`` set. Returning the factory
        # id (the previous behavior) would have the caller start a run
        # against the factory preset, acquiring the wrong wells (or all
        # 96) from live hardware the operator did not authorize for that
        # layout. The zero-acquisition case is normally caught upstream in
        # :meth:`_execute_generated_protocol` before clone is reached, but
        # this method must fail closed on its own contract so any future
        # caller — or a regression in the upstream guard — cannot reach a
        # factory-protocol run via this path. Raise so the caller's
        # try/except converts it into a fail-closed job (``cloned_proto_id``
        # stays 0 on raise so no clone cleanup fires).
        if not protocol_id or not wells:
            reason = "no matched protocol_id" if not protocol_id else "no acquired wells in layout"
            message = f"Refusing protocol clone for {protocol_name}: {reason}"
            job.add_event("protocol_clone_refused", reason)
            logger.warning("%s", message)
            raise ValueError(message)
        new_id = int(time.time()) % 100000 + 2001000
        try:
            self.vm_agent.clone_protocol(protocol_id, new_id, f"ELAB-Run-{new_id}")
        except Exception as error:
            job.add_event("protocol_clone_failed", str(error))
            logger.warning("Protocol clone failed for %s: %s", protocol_name, error)
            raise
        # Reason: clone succeeded → partial protocol exists on the instrument.
        # If the plate-map apply then fails we must clean up that partial
        # clone before re-raising, otherwise the instrument is left with a
        # stub protocol that will float in the MDB until manual cleanup and
        # may shadow the factory protocol on the next assay lookup.
        try:
            self.vm_agent.update_plate_map(new_id, wells)
        except Exception as error:
            job.add_event("plate_map_apply_failed", str(error))
            logger.warning(
                "Plate map apply failed for clone %s of %s: %s",
                new_id,
                protocol_name,
                error,
            )
            self._cleanup_cloned_protocol(new_id)
            raise
        job.add_event("protocol_cloned", f"id={new_id} wells={len(wells)}")
        return new_id, new_id

    def _execute_generated_protocol(self, job: Job) -> None:
        """Run a generated protocol from hash-verified method/layout/analysis refs."""
        specs = self._load_generated_specs(job)
        if specs is None:
            return
        method_spec, layout_spec, analysis_spec = specs

        # Reason: hash verification only proves the bytes match the signed
        # reference. The spec must still conform to a supported schema
        # version with valid well roles/names, or the executor cannot
        # interpret it. Validate before dry-run success and any hardware
        # start (docs/plans/wallac-protocol-authoring.md "Validated workflow").
        if not self._validate_generated_specs(job, method_spec, layout_spec, analysis_spec):
            return

        wells, measured_well_names = self._measured_layout_wells(job, layout_spec)
        # Reason: a zero-acquisition layout (no wells at all, or every well
        # ``skipped``) produces an empty MDB PlateMap. Letting it through
        # would report ``dry_run_complete`` for a no-op run, and in wet
        # mode ask :meth:`_clone_for_layout` to clone with no wells (the
        # factory fallback). Fail closed *before* dry-run success, protocol
        # matching/clone, or any hardware start, mirroring the no-physical-
        # work guarantee for an unintelligible spec.
        if not wells:
            self._fail_job(
                job,
                "Layout has zero acquired wells (empty or all-skipped); "
                "cannot run a protocol with no measurements.",
            )
            job.add_event("layout_no_acquired_wells", "")
            return

        if self.dry_run:
            job.status = "completed"
            job.add_event("dry_run_complete", "Specs validated, would run on instrument")
            return

        protocol_name, protocol_id = self._match_protocol_from_method(method_spec)
        if not protocol_name:
            self._fail_job(job, "Could not match method spec to an instrument protocol")
            return
        job.add_event("protocol_matched", f"{protocol_name} (id={protocol_id})")

        # Reason: _clone_for_layout no longer silently falls back to the
        # factory protocol_id when clone or plate-map apply fails (that
        # fallback ran the instrument against the wrong plate map,
        # breaking the no-physical-work guarantee for an incomplete
        # per-plate setup). It re-raises after best-effort cleanup; the
        # job must fail here rather than run from an orphaned factory
        # clone. cloned_proto_id stays 0 on raise so the caller's finally
        # does not double-delete a clone _clone_for_layout already
        # cleaned up.
        try:
            run_protocol, cloned_proto_id = self._clone_for_layout(
                job, protocol_name, protocol_id, wells
            )
        except Exception as error:
            self._fail_job(
                job,
                f"Protocol clone or plate-map apply failed: {error}",
            )
            return
        # Persist the effective instrument identity used by result lookup. A
        # generated run may use a temporary clone rather than the matched
        # factory protocol, so the clone ID is authoritative for MDB results.
        job.protocol_name = protocol_name
        job.protocol_id = run_protocol
        if not self._snapshot_max_assay_id(job):
            self._fail_job(job, "Could not safely snapshot assays before starting run")
            self._cleanup_cloned_protocol(cloned_proto_id)
            return
        try:
            run_id = self._start_job_run(job, run_protocol)
            if not run_id:
                return
            job.live_wells = []
            self._poll_run(job, run_id, measured_well_names)
            if job.status in ("failed", "aborted"):
                return
            self._fetch_and_writeback(job, run_id, layout_spec, analysis_spec)
        finally:
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

    def _try_abort(self, job: Job, run_id: str) -> bool:
        """Send an abort request; return whether further retries should stop.

        Returns True once no further abort retry should be attempted — either
        because the vm-agent accepted the abort (``ok`` true, ``is_running``
        false, or a stopped state), or because the abort failed permanently
        (non-425 error or an HTTP-200 ``ok=false/is_running=true`` that
        indicates the instrument is still running). A non-accepted
        ``ok=false/is_running=true`` reply is treated as "still in progress"
        so the poll loop keeps trying and a measured/completed run is not
        mislabeled aborted or skipped for writeback.
        """
        try:
            response = self.vm_agent.abort_run(run_id)
        except VmAgentError as error:
            if error.status_code == 425:
                # Too early: retry on the next poll cycle.
                return False
            self._fail_job(job, f"Abort failed permanently: {error}")
            job.add_event("abort_failed", str(error))
            return True
        except Exception as error:
            self._fail_job(job, f"Abort failed permanently: {error}")
            job.add_event("abort_failed", str(error))
            return True

        accepted = bool((response or {}).get("ok", True)) and not bool(
            (response or {}).get("is_running", False)
        )
        if accepted:
            job.add_event("abort_sent", run_id)
            return True
        # Instrument is still running per vm-agent — keep polling so a
        # measured/completed run is not mislabeled aborted. Emit a debug
        # event so the operator can see the controller is still trying.
        job.add_event("abort_in_progress", str((response or {}).get("state_text", "")))
        return False

    def _get_run_state(self, job: Job, run_id: str) -> str | None:
        """Fetch normalized run state, failing the job when polling fails."""
        try:
            run = self.vm_agent.get_run(run_id)
        except VmAgentError as error:
            self._fail_job(job, f"Failed to poll run status: {error}")
            return None
        return str(run.get("state", "")).lower()

    def _refresh_live_wells(
        self,
        job: Job,
        run_id: str,
        measured_wells: set[str],
    ) -> None:
        """Merge the latest valid live readings into the job snapshot."""
        try:
            live = self.vm_agent.get_run_results(run_id)
            wells = live.get("wells", live.get("data", []))
            existing = {well["well"]: well for well in job.live_wells}
            for well in wells:
                name = _normalize_well_name(well.get("well", well.get("well_name", "")))
                if not name or name[0] not in "ABCDEFGH":
                    continue
                if measured_wells and name not in measured_wells:
                    continue
                existing[name] = {
                    "well": name,
                    "od": well.get("od"),
                    "counts": well.get("counts"),
                }
            job.live_wells = list(existing.values())
        except Exception:
            logger.debug("Live result fetch failed for job %s", job.job_id, exc_info=True)

    @staticmethod
    def _record_live_progress(job: Job, measured_wells: set[str], now: float) -> None:
        """Emit a throttled progress event from accumulated live wells."""
        well_count = len(job.live_wells)
        if well_count > 0 and int(now * 3) % 3 == 0:
            total = len(measured_wells) if measured_wells else 96
            job.add_event("run_progress", f"{well_count}/{total} wells measured")

    def _handle_terminal_run_state(
        self,
        job: Job,
        state: str,
        abort_tried: bool,
    ) -> bool:
        """Apply a terminal vm-agent state and report whether polling is done.

        A job already marked terminal (e.g. ``failed`` from a permanent abort
        failure in :meth:`_try_abort`) is left untouched: a subsequent
        instrument state must not be reported as a successful abort.
        """
        if job.status in ("failed", "aborted"):
            return True
        if state in ("measured", "completed", "done", "finished"):
            if job.abort_requested and abort_tried:
                job.status = "aborted"
                job.add_event("execution_aborted", "run stopped")
            else:
                job.add_event("run_completed", state)
            return True
        if state not in ("error", "failed", "aborted"):
            return False
        if job.abort_requested and abort_tried and state == "aborted":
            job.status = "aborted"
            job.add_event("execution_aborted")
        else:
            self._fail_job(job, f"Instrument run failed: state={state}")
        return True

    def _poll_run(self, job: Job, run_id: str, measured_wells: set[str] | None = None) -> None:
        """Poll vm-agent for run completion. Updates job.status.

        Also fetches live results every few seconds so the Run Builder
        can display a real-time heatmap of wells as they are measured.
        The vm-agent skips stale wells from the previous run's live buffer.

        Abort handling: the vm-agent rejects aborts for runs younger than
        60s (aborting earlier wedges the instrument). If abort is requested
        while the run is too young, we keep polling and retry once the
        60s threshold is reached. If the run completes before then, we
        accept the result.
        """
        measured = measured_wells or set()
        deadline = time.monotonic() + POLL_TIMEOUT
        last_live_fetch = 0.0
        abort_tried = False
        while time.monotonic() < deadline:
            if job.abort_requested and not abort_tried:
                abort_tried = self._try_abort(job, run_id)

            state = self._get_run_state(job, run_id)
            if state is None:
                return
            now = time.monotonic()
            if now - last_live_fetch >= 1.0:
                last_live_fetch = now
                self._refresh_live_wells(job, run_id, measured)
                self._record_live_progress(job, measured, now)
            if self._handle_terminal_run_state(job, state, abort_tried):
                return
            time.sleep(POLL_INTERVAL)

        self._fail_job(job, f"Run timed out after {POLL_TIMEOUT}s")

    def _fetch_assay_wells(self, job: Job) -> list[dict[str, Any]]:
        """Retry MDB assay resolution and return the first complete well set."""
        # Reason: protocol_id takes precedence over a potentially stale name,
        # but the lookup must still reject unrelated concurrent assays.
        for attempt in range(10):
            assay_id = _find_assay_after(
                self.vm_agent,
                job.max_assay_before,
                job.protocol_name or "",
                job.protocol_id,
            )
            if assay_id:
                try:
                    results = self.vm_agent.get_job_results(assay_id)
                    wells = results.get("wells", results.get("data", []))
                    if wells:
                        job.assay_prot_id = assay_id
                        job.add_event(
                            "assay_id_resolved", f"{assay_id} (post-run, attempt {attempt + 1})"
                        )
                        return wells
                except VmAgentError:
                    logger.debug("MDB assay lookup retry", exc_info=True)
            if attempt > 0 and attempt % 5 == 0:
                job.add_event(
                    "fetching_results",
                    f"Fetching results (attempt {attempt + 1})",
                )
            time.sleep(3.0)
        return []

    def _fallback_results(self, job: Job, run_id: str) -> list[dict[str, Any]]:
        """Use accumulated live readings, then the run endpoint, as fallback."""
        job.add_event(
            "assay_id_resolution_failed",
            f"No new assay found after max_assay_before={job.max_assay_before}",
        )
        if job.live_wells:
            wells = [
                {
                    "well": reading.get("well", ""),
                    "od": reading.get("od"),
                    "counts": reading.get("counts"),
                }
                for reading in job.live_wells
            ]
            job.add_event("results_from_live_wells", f"{len(wells)} wells (MDB not flushed)")
            return wells
        try:
            results = self.vm_agent.get_run_results(run_id)
            return results.get("wells", results.get("data", []))
        except VmAgentError:
            logger.debug("Run-level results fallback failed", exc_info=True)
            return []

    @staticmethod
    def _normalize_results(
        job: Job,
        wells: list[dict[str, Any]],
        layout_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Normalize addresses and filter persisted placeholders by layout."""
        normalized_wells = []
        for well in wells:
            normalized = dict(well)
            name = normalized.get("well", "")
            if name:
                normalized["well"] = _normalize_well_name(name)
            normalized_wells.append(normalized)
        if layout_spec:
            measured_names = {
                _normalize_well_name(layout_well.get("well_name", layout_well.get("name", "")))
                for layout_well in layout_spec.get("wells", [])
                if layout_well.get("role", "measured") in ("measured", "excluded")
            }
            normalized_wells = [
                well for well in normalized_wells if well.get("well", "") in measured_names
            ]
            job.add_event("results_filtered", f"{len(normalized_wells)} measured wells")
        return normalized_wells

    def _analyze_results(
        self,
        job: Job,
        raw_wells: list[dict[str, Any]],
        layout_spec: dict[str, Any],
        analysis_spec_dict: dict[str, Any],
    ) -> tuple[str, bool]:
        """Run configured analysis, returning ``(csv, analysis_failed)``.

        ``analysis_failed`` is True only when analysis was *requested*
        (both ``layout_spec`` and ``analysis_spec_dict`` present) and the
        pipeline raised. Abstention — no analysis requested — yields
        ``("", False)`` so callers can distinguish it from a real failure
        (docs/abort-recovery.md: requested analysis failing must not be
        reported as a clean completion).
        """
        if not layout_spec or not analysis_spec_dict:
            return "", False
        try:
            layout_wells = {
                _normalize_well_name(well.get("well_name", well.get("name", ""))): well
                for well in layout_spec.get("wells", [])
                if well.get("well_name", well.get("name", ""))
            }
            result = self.analysis.run(
                raw_wells,
                layout_wells,
                AnalysisSpec.from_dict(analysis_spec_dict),
            )
            job.add_event("analysis_complete", f"{len(result.wells)} wells analyzed")
            return result.to_analyzed_wells_csv(), False
        except Exception as error:
            job.add_event("analysis_failed", str(error))
            logger.warning("Analysis failed for job %s: %s", job.job_id, error)
            return "", True

    def _fetch_and_writeback(
        self,
        job: Job,
        run_id: str,
        layout_spec: dict[str, Any] | None = None,
        analysis_spec_dict: dict[str, Any] | None = None,
    ) -> None:
        """Fetch results from vm-agent, run analysis, and write back to eLabFTW."""
        layout = layout_spec or {}
        analysis = analysis_spec_dict or {}
        job.add_event("fetching_results", run_id)
        raw_wells = self._fetch_assay_wells(job)
        if not raw_wells:
            raw_wells = self._fallback_results(job, run_id)
        job.add_event("results_fetched", f"{len(raw_wells)} wells")
        raw_wells = self._normalize_results(job, raw_wells, layout)
        analyzed_csv, analysis_failed = self._analyze_results(job, raw_wells, layout, analysis)

        # A measured/completed run that yields no well records at all is an
        # ambiguous physical state — there is no explicit zero-well contract,
        # so clean completion would be a lie. A legitimate zero reading is a
        # well record with value 0.0 and stays completed; this branch only
        # fires when the MDB/live/run fallbacks returned nothing. See
        # docs/abort-recovery.md "operator review if ambiguous".
        if not raw_wells:
            self._mark_operator_review(
                job,
                "No well results retrieved after run reached measured/completed state",
                detail=f"run_id={run_id}",
            )
            return

        self._writeback(job, raw_wells, analyzed_csv)

        # Requested analysis raised: raw results were written back so the
        # operator can inspect them, but the job is not cleanly completed.
        # A write-back failure keeps the job ``failed`` — that stronger
        # terminal state wins.
        if analysis_failed and job.status == "completed":
            self._mark_operator_review(
                job,
                "Requested analysis failed; raw results written back for review",
                detail="see analysis_failed event",
            )
            return

        # Terminal completion event is emitted only once the operator-review
        # promotion decision above is final, so event consumers never see
        # execution_completed for a job that ends in
        # unknown_requires_operator_review. A failed write-back already
        # recorded writeback_failed and stays failed.
        if job.status == "completed":
            job.add_event("execution_completed", "")

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

            # Reason: writeback success and terminal completion are distinct
            # signals. status is set to completed here so _fetch_and_writeback
            # can still promote to unknown_requires_operator_review when a
            # requested analysis raised; the execution_completed event is
            # emitted only after that promotion decision is final, so event
            # consumers never observe execution_completed for an operator-
            # review job. See docs/abort-recovery.md.
            if job.status != "failed":
                job.status = "completed"
            job.add_event("writeback_completed", f"experiment={exp_id}")

        except Exception as e:
            job.status = "failed"
            job.error = f"Write-back failed: {e}"
            job.add_event("writeback_failed", str(e))
            logger.exception("Write-back failed for job %s", job.job_id)

    def _build_results_html(self, job: Job, raw_wells: list[dict[str, Any]]) -> str:
        """Build a rich HTML body with plate heatmap and results table."""
        well_values = _well_values(raw_wells)
        vals = list(well_values.values())
        vmin = min(vals) if vals else 0.0
        vmax = max(vals) if vals else 1.0
        vrange = vmax - vmin if vmax > vmin else 1.0
        n_measured = len(well_values)
        mean_val = sum(vals) / len(vals) if vals else 0
        min_val = vmin if vals else 0
        max_val = vmax if vals else 0
        return (
            f"<h2>Wallac Victor2 Results</h2>"
            f"<table style='border-collapse:collapse; margin-bottom:16px;'>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Job ID:</td><td>{html.escape(job.job_id)}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Protocol:</td><td>{html.escape(job.protocol_name or 'N/A')}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Run ID:</td><td>{html.escape(job.run_id)}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Wells measured:</td><td>{n_measured}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0; font-weight:bold;'>Min / Mean / Max:</td><td>{min_val:.4f} / {mean_val:.4f} / {max_val:.4f}</td></tr>"
            f"</table>"
            f"<h3>Plate Heatmap</h3>"
            f"{_plate_heatmap_html(well_values, vmin, vrange)}"
            f"<h3>Results (Top Wells)</h3>"
            f"{_top_results_html(well_values)}"
            f"<p style='margin-top:16px; font-size:0.85rem; color:#666;'>"
            f"Raw results (JSON) and analyzed data (CSV) are attached as files below."
            f"</p>"
        )

    def _download_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Download a canonical JSON attachment from eLabFTW using a ref dict, verifying hash.

        Ref format: {"object_id": int, "hash": str, "json_attachment_id" or "attachment_id": int}

        Raises:
            BridgeError: with code SIGNATURE_MISSING if ref fields are incomplete.
            BridgeError: with code CANONICAL_HASH_MISMATCH if the downloaded bytes
                do not hash to the expected value.  This is a fail-closed check.
        """
        object_id = ref.get("object_id", 0)
        # Support both json_attachment_id (canonical schema name) and
        # attachment_id (legacy HTTP API name) for backward compatibility.
        attachment_id = ref.get("json_attachment_id", 0) or ref.get("attachment_id", 0)
        expected_hash = ref.get("hash", "")

        if not object_id or not attachment_id:
            raise BridgeError(
                code=SIGNATURE_MISSING,
                severity=Severity.ERROR,
                human_message=(f"Missing object_id or attachment_id in {list(ref.keys())}: {ref}"),
                operator_hint=(
                    "The method/layout/analysis reference must include object_id and "
                    "attachment_id (or json_attachment_id) for hash-verified download."
                ),
                retryable=False,
                details={"ref": ref},
            )

        if not expected_hash:
            raise BridgeError(
                code=SIGNATURE_MISSING,
                severity=Severity.ERROR,
                human_message="Missing hash in reference: cannot verify attachment integrity.",
                operator_hint=(
                    "The method/layout/analysis reference must include a SHA-256 hash "
                    "of the canonical JSON attachment for integrity verification."
                ),
                retryable=False,
                details={"ref_keys": list(ref.keys())},
            )

        try:
            data = self.elabftw.download_upload(object_id, attachment_id)
        except Exception as e:
            raise BridgeError(
                code=SIGNATURE_MISSING,
                severity=Severity.ERROR,
                human_message=f"Failed to download attachment {attachment_id} from object {object_id}: {e}",
                operator_hint="Check that the attachment exists and the eLabFTW API is reachable.",
                retryable=True,
                details={"object_id": object_id, "attachment_id": attachment_id},
            ) from e

        try:
            verify_hash(data, expected_hash)
        except BridgeError as error:
            actual_hash = str(error.details.get("actual_hash", ""))
            raise BridgeError(
                code=error.code,
                severity=error.severity,
                human_message=(
                    f"Hash mismatch for downloaded attachment: expected {expected_hash}, got {actual_hash}. "
                    "The attachment may have been replaced or corrupted after submission."
                ),
                operator_hint=(
                    "Re-generate the spec reference, or restore the original "
                    "referenced version in eLabFTW."
                ),
                retryable=error.retryable,
                details={
                    **error.details,
                    "actual_hash": actual_hash,
                    "object_id": object_id,
                    "attachment_id": attachment_id,
                },
            ) from error

        return json.loads(data)

    def _match_protocol_from_method(self, method_spec: dict[str, Any]) -> tuple[str, int]:
        """Match a method spec to a protocol on the instrument.

        Returns (protocol_name, protocol_id), or ("", 0) if no match found.
        Prefers factory presets over custom protocols to avoid matching
        leftover clones from previous runs.
        """
        try:
            response = self.vm_agent.get_protocols(refresh=True)
            protocols = (
                response.get("protocols", response) if isinstance(response, dict) else response
            )
        except Exception:
            return "", 0
        matcher = {
            "photometry": _match_photometry,
            "fluorometry": _match_fluorometry,
            "luminescence": _match_luminescence,
        }.get(str(method_spec.get("mode", "")))
        return matcher(method_spec, protocols) if matcher else ("", 0)
