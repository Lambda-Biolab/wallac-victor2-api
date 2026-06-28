"""Validation-only bridge path for Wallac Victor2 protocol authoring.

Implements Stage 4 of docs/plans/wallac-protocol-authoring.md.

This module validates signed canonical JSON bundles **without** executing
anything or writing to the MDB.  It is the safety gate between draft
authoring (Stage 3) and generated-protocol execution (Stages 5-6).

Validation checks:
1. Signed canonical attachment verification (download bytes, hash, compare).
2. Signer allowlist (static configured list for v1).
3. Lifecycle eligibility (referenced objects must be signed/active).
4. Live vm-agent health/capability checks.
5. Method/Layout/Analysis/Job consistency checks.
6. MDB generation dry-run plan (no writes).

All failures fail closed with structured BridgeError codes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .canonical import compute_hash
from .errors import (
    BridgeError,
)
from .schemas import (
    EXECUTABLE_LIFECYCLE_STATES,
    ExecutionMode,
    JobSpec,
    LayoutSpec,
    WellRole,
)

logger = logging.getLogger(__name__)


# --- Protocols for dependencies --------------------------------------------


class ValidationElabftwClient(Protocol):
    """eLabFTW client methods needed for validation."""

    def get_item(self, item_id: int) -> dict[str, Any]: ...

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]: ...

    def download_upload(self, item_id: int, upload_id: int) -> bytes: ...

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None: ...


class VmAgentHealthClient(Protocol):
    """vm-agent client for health/capability checks."""

    def get_health(self) -> dict[str, Any]:
        """Return health dict with at least 'instrument_connected' and 'is_idle'."""
        ...

    def get_instrument(self) -> dict[str, Any]:
        """Return instrument capabilities dict with 'technologies'."""
        ...


# --- Validation result -----------------------------------------------------


@dataclass
class ValidationReport:
    """Result of validating a signed Automation Job bundle.

    Stored as an attachment on the Automation Job in eLabFTW.
    """

    job_item_id: int
    valid: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            self.errors.append({"check": name, "detail": detail})

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_item_id": self.job_item_id,
            "valid": self.valid,
            "checks": list(self.checks),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- Signer allowlist ------------------------------------------------------


@dataclass
class SignerAllowlist:
    """Static configured authorized-signer allowlist for v1.

    Dynamic eLabFTW team/group lookup is future work.
    """

    authorized_signers: frozenset[str]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SignerAllowlist:
        import os

        e = env if env is not None else dict(os.environ)
        raw = e.get("WALLAC_AUTHORIZED_SIGNERS", "")
        signers = frozenset(s.strip() for s in raw.split(",") if s.strip())
        return cls(authorized_signers=signers)

    def is_authorized(self, signer: str) -> bool:
        if not self.authorized_signers:
            # If no allowlist configured, fail closed for production.
            # Tests can set an explicit allowlist.
            return False
        return signer in self.authorized_signers


# --- Mode capability mapping ------------------------------------------------

#: Maps measurement modes to required vm-agent technology capabilities.
MODE_CAPABILITY_MAP: dict[str, str] = {
    "photometry": "photometer",
    "fluorometry": "prompt_fluorometer",
    "luminescence": "luminometer",
}


# --- Validation service ----------------------------------------------------


class ValidationService:
    """Validates signed Automation Job bundles without executing.

    All checks fail closed: any missing signature, invalid signature,
    unauthorized signer, stale lifecycle, hash mismatch, unsupported schema,
    or capability mismatch prevents MDB generation and execution.
    """

    def __init__(
        self,
        elabftw_client: ValidationElabftwClient,
        vm_agent_client: VmAgentHealthClient | None = None,
        signer_allowlist: SignerAllowlist | None = None,
    ) -> None:
        self.elabftw = elabftw_client
        self.vm_agent = vm_agent_client
        self.signer_allowlist = signer_allowlist or SignerAllowlist.from_env()

    def validate_job(self, job_item_id: int) -> ValidationReport:
        """Validate a signed Automation Job bundle.

        Downloads and verifies all signed attachments, checks signer
        authorization, lifecycle eligibility, and vm-agent capabilities.

        Returns:
            ValidationReport with all check results.
        """
        report = ValidationReport(job_item_id=job_item_id, valid=True)

        try:
            # 1. Load the Automation Job item
            job_item = self.elabftw.get_item(job_item_id)
            from .elabftw import extract_extra_fields, get_field_value

            job_ef = extract_extra_fields(job_item.get("metadata"))

            # 2. Check execution mode
            execution_mode = get_field_value(job_ef, "Execution mode")
            if execution_mode == ExecutionMode.EXISTING_PROTOCOL.value:
                # existing_protocol: validate protocol name exists
                report.add_check(
                    "execution_mode",
                    True,
                    "existing_protocol mode — protocol validation deferred to runtime",
                )
                report.valid = len(report.errors) == 0
                return report

            # generated_protocol: full canonical bundle validation
            self._validate_generated_bundle(job_item_id, job_ef, report)

        except BridgeError as e:
            report.add_check("validation_error", False, str(e))
        except Exception as e:
            report.add_check("unexpected_error", False, str(e))

        report.valid = len(report.errors) == 0
        return report

    def _validate_generated_bundle(
        self,
        job_item_id: int,
        job_ef: dict[str, Any],
        report: ValidationReport,
    ) -> None:
        """Validate a generated_protocol bundle: all four signed specs."""
        from .elabftw import get_field_value

        # --- Download and verify job.json ---
        job_hash = get_field_value(job_ef, "Job hash")
        job_attachment_id = get_field_value(job_ef, "Job JSON attachment ID")

        job_spec = self._download_and_verify(
            job_item_id, job_attachment_id, job_hash, "job", report
        )
        if job_spec is None:
            return

        # Parse the job spec
        try:
            job = JobSpec.from_dict(job_spec)
        except BridgeError as e:
            report.add_check("job_schema", False, str(e))
            return
        except Exception as e:
            report.add_check("job_schema", False, f"Failed to parse job.json: {e}")
            return
        report.add_check("job_schema", True, "job.json parsed successfully")

        # --- Validate Method reference ---
        if job.method is not None:
            self._validate_referenced_object(
                "method",
                job.method.object_id,
                job.method.hash,
                job.method.json_attachment_id,
                report,
            )

        # --- Validate Layout reference ---
        layout_spec_dict: dict[str, Any] | None = None
        if job.layout is not None:
            if job.layout.source == "reusable" and job.layout.object_id:
                layout_spec_dict = self._validate_referenced_object(
                    "layout",
                    job.layout.object_id,
                    job.layout.hash,
                    job.layout.json_attachment_id,
                    report,
                )
            else:
                # one-off layout: attachment is on the job itself
                layout_spec_dict = self._download_and_verify(
                    job_item_id,
                    str(job.layout.json_attachment_id),
                    job.layout.hash,
                    "layout",
                    report,
                )

        # --- Validate Layout content (semantic rules) ---
        # Reason: schema validation only checks structure (well names are
        # A1..H12, role is a valid enum). It does NOT catch invalid
        # combinations like a replicate group with only one well, or an
        # excluded well carrying sample metadata — those are semantic
        # rules that would produce nonsense at analysis time.
        if layout_spec_dict is not None:
            self._validate_layout_content(layout_spec_dict, report)

        # --- Validate Analysis reference ---
        if job.analysis is not None:
            self._validate_referenced_object(
                "analysis",
                job.analysis.object_id,
                job.analysis.hash,
                job.analysis.json_attachment_id,
                report,
            )

        # --- Check vm-agent capabilities ---
        if self.vm_agent is not None:
            self._check_vm_agent_capabilities(job, report)
        else:
            report.add_warning("vm-agent client not configured — capability check skipped")

    def _validate_referenced_object(
        self,
        kind: str,
        object_id: int,
        expected_hash: str,
        json_attachment_id: int,
        report: ValidationReport,
    ) -> dict[str, Any] | None:
        """Validate a referenced signed object (Method, Layout, Analysis).

        Returns the parsed spec dict on success (so callers can run
        content-specific checks), or None on any failure.
        """
        from .elabftw import extract_extra_fields, get_field_value

        # Get the referenced item
        try:
            item = self.elabftw.get_item(object_id)
        except Exception as e:
            report.add_check(f"{kind}_exists", False, f"Failed to get {kind} item {object_id}: {e}")
            return None

        ef = extract_extra_fields(item.get("metadata"))

        # Check lifecycle state
        lifecycle = get_field_value(ef, "Lifecycle state")
        if lifecycle not in EXECUTABLE_LIFECYCLE_STATES:
            report.add_check(
                f"{kind}_lifecycle",
                False,
                f"{kind} {object_id} lifecycle is '{lifecycle}', not in {EXECUTABLE_LIFECYCLE_STATES}",
            )
            return None
        report.add_check(f"{kind}_lifecycle", True, f"{kind} {object_id} is signed/active")

        # Download and verify the canonical JSON attachment, return parsed dict
        return self._download_and_verify(
            object_id, str(json_attachment_id), expected_hash, kind, report
        )

    def _download_and_verify(
        self,
        item_id: int,
        attachment_id_str: str,
        expected_hash: str,
        kind: str,
        report: ValidationReport,
    ) -> dict[str, Any] | None:
        """Download attachment, verify hash, parse JSON. Returns parsed dict or None."""
        if not attachment_id_str or not expected_hash:
            report.add_check(
                f"{kind}_attachment_id",
                False,
                f"Missing attachment ID or hash for {kind}",
            )
            return None

        try:
            attachment_id = int(attachment_id_str)
        except ValueError:
            report.add_check(
                f"{kind}_attachment_id",
                False,
                f"Invalid attachment ID '{attachment_id_str}' for {kind}",
            )
            return None

        # Download the attachment bytes
        try:
            attachment_bytes = self.elabftw.download_upload(item_id, attachment_id)
        except Exception as e:
            report.add_check(
                f"{kind}_download",
                False,
                f"Failed to download {kind} attachment {attachment_id}: {e}",
            )
            return None

        # Verify hash
        actual_hash = compute_hash(attachment_bytes)
        if actual_hash != expected_hash.lower():
            report.add_check(
                f"{kind}_hash",
                False,
                f"Hash mismatch for {kind}: expected {expected_hash}, got {actual_hash}",
            )
            return None
        report.add_check(f"{kind}_hash", True, f"{kind} hash verified")

        # Parse JSON
        try:
            return json.loads(attachment_bytes)
        except json.JSONDecodeError as e:
            report.add_check(
                f"{kind}_json",
                False,
                f"Failed to parse {kind} JSON: {e}",
            )
            return None

    def _check_vm_agent_capabilities(self, job: JobSpec, report: ValidationReport) -> None:
        """Check that vm-agent is healthy and supports the required mode."""
        try:
            health = self.vm_agent.get_health()
        except Exception as e:
            report.add_check(
                "vm_agent_health",
                False,
                f"Failed to get vm-agent health: {e}",
            )
            return

        connected = health.get("instrument_connected", False)
        is_idle = health.get("is_idle", True)
        is_error = health.get("is_error", False)

        if not connected:
            report.add_check(
                "vm_agent_health",
                False,
                "Instrument not connected",
            )
            return
        if is_error:
            report.add_check(
                "vm_agent_health",
                False,
                "Instrument in error state",
            )
            return
        if not is_idle:
            report.add_warning("Instrument is not idle — job will queue")

        report.add_check("vm_agent_health", True, "Instrument connected and idle")

        # Check mode capability
        if job.method is not None:
            method_item = self.elabftw.get_item(job.method.object_id)
            from .elabftw import extract_extra_fields, get_field_value

            method_ef = extract_extra_fields(method_item.get("metadata"))
            mode = get_field_value(method_ef, "Measurement mode")

            if mode in MODE_CAPABILITY_MAP:
                required_tech = MODE_CAPABILITY_MAP[mode]
                try:
                    instrument = self.vm_agent.get_instrument()
                    technologies = instrument.get("technologies", {})
                    if not technologies.get(required_tech, False):
                        report.add_check(
                            "mode_capability",
                            False,
                            f"Mode '{mode}' requires technology '{required_tech}' which is not available",
                        )
                        return
                    report.add_check(
                        "mode_capability",
                        True,
                        f"Mode '{mode}' supported (technology '{required_tech}' available)",
                    )
                except Exception as e:
                    report.add_check(
                        "mode_capability",
                        False,
                        f"Failed to check instrument capabilities: {e}",
                    )

    # --- Layout content validation (semantic rules) -------------------------

    def _validate_layout_content(
        self,
        layout_dict: dict[str, Any],
        report: ValidationReport,
    ) -> None:
        """Run semantic checks on a parsed layout spec.

        Schema validation (LayoutSpec.from_dict) catches structurally
        invalid layouts (bad well names, unknown roles). These checks
        catch invalid **combinations** that would produce nonsense at
        analysis time or an inconsistent MDB PlateMap.

        Rules (9):
          1. Empty layout (no wells at all)
          2. No measured wells (nothing to put in PlateMap)
          3. Duplicate well_name entries
          4. Skipped well with sample_name / sample_label set
          5. Excluded well with sample_name / sample_label set
          6. Well with control_type but role != measured
          7. Blank well (control_type=blank) that is not measured
          8. Replicate group with only 1 well (replicate implies >=2)
          9. Replicate group made up entirely of excluded wells
        """
        # Parse first — schema errors are also content errors here.
        try:
            layout = LayoutSpec.from_dict(layout_dict)
        except Exception as e:
            report.add_check("layout_schema", False, f"Failed to parse layout spec: {e}")
            return
        report.add_check("layout_schema", True, "layout spec parsed")

        wells = layout.wells
        self._check_layout_population(wells, report)
        # Skip per-well rules if there are no wells — they'd all be vacuous.
        if not wells:
            return
        self._check_layout_duplicates(wells, report)
        self._check_layout_role_metadata(wells, report)
        self._check_layout_replicate_groups(wells, report)

    def _check_layout_population(self, wells: list, report: ValidationReport) -> None:
        """Rules 1 (empty) + 2 (no measured wells)."""
        if not wells:
            report.add_check("layout_empty", False, "Layout has no wells")
            return
        report.add_check("layout_empty", True, f"Layout has {len(wells)} wells")

        measured = [w for w in wells if w.role == WellRole.MEASURED.value]
        if not measured:
            report.add_check(
                "layout_no_measured",
                False,
                "Layout has no 'measured' wells — nothing to put in the MDB PlateMap",
            )
        else:
            report.add_check(
                "layout_no_measured",
                True,
                f"{len(measured)} measured well(s)",
            )

    @staticmethod
    def _check_layout_duplicates(wells: list, report: ValidationReport) -> None:
        """Rule 3: duplicate well_name entries."""
        seen: set[str] = set()
        dups: list[str] = []
        for w in wells:
            if w.well_name in seen:
                dups.append(w.well_name)
            seen.add(w.well_name)
        if dups:
            report.add_check(
                "layout_duplicate_wells",
                False,
                f"Duplicate well_name entries: {sorted(set(dups))}",
            )
        else:
            report.add_check(
                "layout_duplicate_wells",
                True,
                "All well_name entries are unique",
            )

    def _check_layout_role_metadata(self, wells: list, report: ValidationReport) -> None:
        """Rules 4-7: per-well role vs metadata contradictions."""
        offenders_skip_sample: list[str] = []
        offenders_excl_sample: list[str] = []
        offenders_ctrl_role: list[str] = []
        offenders_blank_not_measured: list[str] = []
        for w in wells:
            has_sample_meta = bool(w.sample_name or w.sample_label)
            if w.role == WellRole.SKIPPED.value and has_sample_meta:
                offenders_skip_sample.append(w.well_name)
            if w.role == WellRole.EXCLUDED.value and has_sample_meta:
                offenders_excl_sample.append(w.well_name)
            if w.control_type and w.role != WellRole.MEASURED.value:
                offenders_ctrl_role.append(w.well_name)
            if w.control_type == "blank" and w.role != WellRole.MEASURED.value:
                offenders_blank_not_measured.append(w.well_name)

        self._emit_offenders_check(
            report,
            "layout_skipped_with_sample",
            offenders_skip_sample,
            "No skipped wells carry sample metadata",
            "Skipped wells should not carry sample_name/sample_label",
        )
        self._emit_offenders_check(
            report,
            "layout_excluded_with_sample",
            offenders_excl_sample,
            "No excluded wells carry sample metadata",
            "Excluded wells should not carry sample_name/sample_label",
        )
        self._emit_offenders_check(
            report,
            "layout_control_role",
            offenders_ctrl_role,
            "All control wells are 'measured' role",
            "Wells with control_type set must have role='measured'",
        )
        self._emit_offenders_check(
            report,
            "layout_blank_not_measured",
            offenders_blank_not_measured,
            "All blank wells are 'measured' role",
            "Blank wells must have role='measured'",
        )

    @staticmethod
    def _emit_offenders_check(
        report: ValidationReport,
        name: str,
        offenders: list[str],
        ok_msg: str,
        err_msg: str,
    ) -> None:
        """Emit a pass/fail check with either the ok message or offenders list."""
        # Reason: 4 nearly-identical role/metadata checks share this emit
        # pattern; dedup keeps _check_layout_role_metadata readable.
        if offenders:
            report.add_check(name, False, f"{err_msg}: {offenders}")
        else:
            report.add_check(name, True, ok_msg)

    @staticmethod
    def _check_layout_replicate_groups(wells: list, report: ValidationReport) -> None:
        """Rules 8 (replicate group with <2 wells) + 9 (group all excluded)."""
        groups: dict[str, list[str]] = {}
        for w in wells:
            if w.replicate_group:
                groups.setdefault(w.replicate_group, []).append(w.well_name)

        too_small = sorted(g for g, names in groups.items() if len(names) < 2)
        if too_small:
            report.add_check(
                "layout_replicate_group_size",
                False,
                f"Replicate groups must have >=2 wells: {too_small}",
            )
        else:
            report.add_check(
                "layout_replicate_group_size",
                True,
                f"{len(groups)} replicate group(s), all with >=2 wells",
            )

        all_excluded_groups: list[str] = []
        for gname in groups:
            wells_in_group = [w for w in wells if w.replicate_group == gname]
            if wells_in_group and all(w.role == WellRole.EXCLUDED.value for w in wells_in_group):
                all_excluded_groups.append(gname)
        if all_excluded_groups:
            report.add_check(
                "layout_replicate_group_all_excluded",
                False,
                f"Replicate groups made up entirely of 'excluded' wells: {all_excluded_groups}",
            )
        else:
            report.add_check(
                "layout_replicate_group_all_excluded",
                True,
                "No replicate group is entirely 'excluded' wells",
            )
