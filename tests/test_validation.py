"""Tests for the validation-only bridge path (Stage 4).

Tests cover:
- Valid bundle passes validation
- Missing/invalid signature fails closed
- Hash mismatch fails closed
- Stale lifecycle fails closed
- Unauthorized signer fails closed
- vm-agent capability checks
- existing_protocol mode (lighter validation)
- Validation report structure
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bridge.canonical import canonicalize_and_hash
from bridge.validation import (
    SignerAllowlist,
    ValidationReport,
    ValidationService,
)

# --- Mock clients ---


class MockValidationElabftwClient:
    """In-memory mock for the eLabFTW client during validation."""

    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._uploads: dict[tuple[int, int], bytes] = {}

    def add_item(
        self,
        item_id: int,
        category_id: int = 9,
        title: str = "Test",
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        ef = extra_fields or {}
        metadata = {"extra_fields": ef}
        self._items[item_id] = {
            "id": item_id,
            "title": title,
            "category": category_id,
            "metadata": json.dumps(metadata),
        }

    def add_upload(self, item_id: int, upload_id: int, content: bytes) -> None:
        self._uploads[(item_id, upload_id)] = content

    def get_item(self, item_id: int) -> dict[str, Any]:
        return dict(self._items[item_id])

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return [
            {"id": uid, "real_name": "test.json"} for (iid, uid) in self._uploads if iid == item_id
        ]

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        return self._uploads.get((item_id, upload_id), b"")

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        item = self._items[item_id]
        meta = (
            json.loads(item["metadata"]) if isinstance(item["metadata"], str) else item["metadata"]
        )
        ef = meta.get("extra_fields") or {}
        ef.update(extra_fields)
        meta["extra_fields"] = ef
        item["metadata"] = json.dumps(meta)


class MockVmAgentClient:
    """Mock vm-agent for health/capability checks."""

    def __init__(
        self,
        connected: bool = True,
        idle: bool = True,
        error: bool = False,
        technologies: dict[str, bool] | None = None,
    ) -> None:
        self._connected = connected
        self._idle = idle
        self._error = error
        self._technologies = technologies or {
            "photometer": True,
            "prompt_fluorometer": True,
            "luminometer": True,
        }

    def get_health(self) -> dict[str, Any]:
        return {
            "instrument_connected": self._connected,
            "is_idle": self._idle,
            "is_error": self._error,
            "ok": self._connected and not self._error,
        }

    def get_instrument(self) -> dict[str, Any]:
        return {"technologies": self._technologies}


# --- Helpers ---


def make_method_spec_dict(mode: str = "photometry") -> dict[str, Any]:
    spec = {
        "schema_name": "wallac.method",
        "schema_version": 1,
        "mode": mode,
        "name": "Test Method",
        "plate_type": "96-well",
    }
    if mode == "photometry":
        spec["photometry"] = {"filter_id": "P610", "filter_name": "610nm", "read_time_seconds": 1.0}
    elif mode == "fluorometry":
        spec["fluorometry"] = {
            "excitation_filter_id": "F485",
            "excitation_filter_name": "485nm",
            "emission_filter_id": "F535",
            "emission_filter_name": "535nm",
            "read_time_seconds": 1.0,
        }
    elif mode == "luminescence":
        spec["luminescence"] = {"integration_time_seconds": 1.0}
    return spec


def make_job_spec_dict(
    method_id: int = 10,
    method_hash: str = "",
    method_attachment_id: int = 5001,
    layout_id: int = 11,
    layout_hash: str = "",
    layout_attachment_id: int = 5002,
    analysis_id: int = 12,
    analysis_hash: str = "",
    analysis_attachment_id: int = 5003,
) -> dict[str, Any]:
    return {
        "schema_name": "wallac.job",
        "schema_version": 1,
        "execution_mode": "generated_protocol",
        "method": {
            "object_id": method_id,
            "hash": method_hash,
            "json_attachment_id": method_attachment_id,
        },
        "layout": {
            "source": "reusable",
            "hash": layout_hash,
            "json_attachment_id": layout_attachment_id,
            "object_id": layout_id,
        },
        "analysis": {
            "object_id": analysis_id,
            "hash": analysis_hash,
            "json_attachment_id": analysis_attachment_id,
        },
    }


def setup_valid_bundle(
    elabftw: MockValidationElabftwClient,
    vm_agent: MockVmAgentClient,
) -> int:
    """Set up a complete valid generated_protocol bundle. Returns job item ID."""
    # Create method item with signed/active lifecycle
    method_spec = make_method_spec_dict("photometry")
    method_bytes, method_hash = canonicalize_and_hash(method_spec)
    elabftw.add_item(
        10,
        category_id=10,
        title="Test Method",
        extra_fields={
            "Lifecycle state": {"value": "signed/active"},
            "Measurement mode": {"value": "photometry"},
            "Method hash": {"value": method_hash},
            "Method JSON attachment ID": {"value": "5001"},
        },
    )
    elabftw.add_upload(10, 5001, method_bytes)

    # Create layout item
    layout_spec = {
        "schema_name": "wallac.layout",
        "schema_version": 1,
        "plate_type": "96-well",
        "wells": [{"well_name": "A1", "role": "measured"}],
    }
    layout_bytes, layout_hash = canonicalize_and_hash(layout_spec)
    elabftw.add_item(
        11,
        category_id=11,
        title="Test Layout",
        extra_fields={
            "Lifecycle state": {"value": "signed/active"},
            "Layout hash": {"value": layout_hash},
            "Layout JSON attachment ID": {"value": "5002"},
        },
    )
    elabftw.add_upload(11, 5002, layout_bytes)

    # Create analysis item
    analysis_spec = {
        "schema_name": "wallac.analysis",
        "schema_version": 1,
        "blank_subtraction": {"enabled": False, "blank_wells": []},
        "replicate_aggregation": {"enabled": False, "group_by": "replicate_group"},
        "normalization": {"enabled": False, "control_type": "", "target_value": 100.0},
        "thresholds": [],
        "exclusions": [],
        "outputs": ["raw_results", "analyzed_wells", "replicate_summary", "analysis_summary"],
    }
    analysis_bytes, analysis_hash = canonicalize_and_hash(analysis_spec)
    elabftw.add_item(
        12,
        category_id=12,
        title="Test Analysis",
        extra_fields={
            "Lifecycle state": {"value": "signed/active"},
            "Analysis hash": {"value": analysis_hash},
            "Analysis JSON attachment ID": {"value": "5003"},
        },
    )
    elabftw.add_upload(12, 5003, analysis_bytes)

    # Create job item
    job_spec = make_job_spec_dict(
        method_hash=method_hash,
        layout_hash=layout_hash,
        analysis_hash=analysis_hash,
    )
    job_bytes, job_hash = canonicalize_and_hash(job_spec)
    elabftw.add_item(
        100,
        category_id=9,
        title="Test Job",
        extra_fields={
            "Execution mode": {"value": "generated_protocol"},
            "Job hash": {"value": job_hash},
            "Job JSON attachment ID": {"value": "5004"},
        },
    )
    elabftw.add_upload(100, 5004, job_bytes)

    return 100


# --- Fixtures ---


@pytest.fixture
def elabftw() -> MockValidationElabftwClient:
    return MockValidationElabftwClient()


@pytest.fixture
def vm_agent() -> MockVmAgentClient:
    return MockVmAgentClient()


@pytest.fixture
def service(
    elabftw: MockValidationElabftwClient,
    vm_agent: MockVmAgentClient,
) -> ValidationService:
    return ValidationService(elabftw, vm_agent)


# --- ValidationReport tests ---


class TestValidationReport:
    def test_empty_report_is_valid(self) -> None:
        report = ValidationReport(job_item_id=1, valid=True)
        assert report.valid is True
        assert len(report.checks) == 0
        assert len(report.errors) == 0

    def test_add_check_passed(self) -> None:
        report = ValidationReport(job_item_id=1, valid=True)
        report.add_check("test", True, "ok")
        assert len(report.checks) == 1
        assert len(report.errors) == 0

    def test_add_check_failed(self) -> None:
        report = ValidationReport(job_item_id=1, valid=True)
        report.add_check("test", False, "failed")
        assert len(report.checks) == 1
        assert len(report.errors) == 1
        assert report.errors[0]["check"] == "test"

    def test_to_json_bytes(self) -> None:
        report = ValidationReport(job_item_id=42, valid=True)
        report.add_check("test", True, "ok")
        data = json.loads(report.to_json_bytes())
        assert data["job_item_id"] == 42
        assert data["valid"] is True
        assert len(data["checks"]) == 1


# --- SignerAllowlist tests ---


class TestSignerAllowlist:
    def test_empty_allowlist_rejects_all(self) -> None:
        al = SignerAllowlist(authorized_signers=frozenset())
        assert al.is_authorized("anyone") is False

    def test_authorized_signer_passes(self) -> None:
        al = SignerAllowlist(authorized_signers=frozenset({"alice", "bob"}))
        assert al.is_authorized("alice") is True
        assert al.is_authorized("bob") is True

    def test_unauthorized_signer_rejected(self) -> None:
        al = SignerAllowlist(authorized_signers=frozenset({"alice"}))
        assert al.is_authorized("eve") is False

    def test_from_env(self) -> None:
        al = SignerAllowlist.from_env({"WALLAC_AUTHORIZED_SIGNERS": "alice, bob ,carol"})
        assert al.is_authorized("alice") is True
        assert al.is_authorized("bob") is True
        assert al.is_authorized("carol") is True
        assert al.is_authorized("dave") is False


# --- ValidationService tests ---


class TestValidationServiceValidBundle:
    def test_valid_bundle_passes(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, vm_agent)
        report = service.validate_job(job_id)

        assert report.valid is True
        assert len(report.errors) == 0
        # Should have checks for: job_hash, job_schema, method_lifecycle,
        # method_hash, layout_lifecycle, layout_hash, analysis_lifecycle,
        # analysis_hash, vm_agent_health, mode_capability
        check_names = [c["name"] for c in report.checks]
        assert "job_hash" in check_names
        assert "job_schema" in check_names
        assert "method_lifecycle" in check_names
        assert "method_hash" in check_names
        assert "layout_lifecycle" in check_names
        assert "analysis_lifecycle" in check_names
        assert "vm_agent_health" in check_names
        assert "mode_capability" in check_names


class TestValidationServiceHashMismatch:
    def test_hash_mismatch_fails_closed(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, vm_agent)

        # Corrupt the job attachment
        elabftw._uploads[(job_id, 5004)] = b'{"different": "content"}'

        report = service.validate_job(job_id)
        assert report.valid is False
        assert any(e["check"] == "job_hash" for e in report.errors)


class TestValidationServiceStaleLifecycle:
    def test_stale_lifecycle_fails_closed(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, vm_agent)

        # Set method lifecycle to draft
        elabftw.patch_metadata(10, {"Lifecycle state": {"value": "draft"}})

        report = service.validate_job(job_id)
        assert report.valid is False
        assert any(e["check"] == "method_lifecycle" for e in report.errors)


class TestValidationServiceExistingProtocol:
    def test_existing_protocol_light_validation(
        self,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        # Set up an existing_protocol job (no canonical bundle)
        elabftw.add_item(
            200,
            category_id=9,
            title="Existing Protocol Job",
            extra_fields={
                "Execution mode": {"value": "existing_protocol"},
                "Protocol name": {"value": "Absorbance 600"},
            },
        )
        service = ValidationService(elabftw, vm_agent)
        report = service.validate_job(200)

        assert report.valid is True
        assert len(report.errors) == 0


class TestValidationServiceVmAgent:
    def test_instrument_not_connected(
        self,
        elabftw: MockValidationElabftwClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, MockVmAgentClient(connected=True, idle=True))
        vm_agent = MockVmAgentClient(connected=False)
        service = ValidationService(elabftw, vm_agent)

        report = service.validate_job(job_id)
        assert report.valid is False
        assert any(e["check"] == "vm_agent_health" for e in report.errors)

    def test_instrument_in_error(
        self,
        elabftw: MockValidationElabftwClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, MockVmAgentClient(connected=True, idle=True))
        vm_agent = MockVmAgentClient(connected=True, error=True)
        service = ValidationService(elabftw, vm_agent)

        report = service.validate_job(job_id)
        assert report.valid is False
        assert any(e["check"] == "vm_agent_health" for e in report.errors)

    def test_mode_capability_missing(
        self,
        elabftw: MockValidationElabftwClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, MockVmAgentClient(connected=True, idle=True))
        # vm-agent doesn't have photometer capability
        vm_agent = MockVmAgentClient(
            connected=True,
            technologies={"photometer": False, "prompt_fluorometer": True, "luminometer": True},
        )
        service = ValidationService(elabftw, vm_agent)

        report = service.validate_job(job_id)
        assert report.valid is False
        assert any(e["check"] == "mode_capability" for e in report.errors)

    def test_no_vm_agent_skips_capability_check(
        self,
        elabftw: MockValidationElabftwClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, MockVmAgentClient(connected=True, idle=True))
        service = ValidationService(elabftw, None)  # no vm-agent

        report = service.validate_job(job_id)
        assert report.valid is True
        assert any("capability check skipped" in w for w in report.warnings)


class TestValidationServiceMissingAttachment:
    def test_missing_job_hash_fails(
        self,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, vm_agent)
        # Remove the hash field
        elabftw.patch_metadata(job_id, {"Job hash": {"value": ""}})

        service = ValidationService(elabftw, vm_agent)
        report = service.validate_job(job_id)
        assert report.valid is False
        assert any("attachment" in e["check"] for e in report.errors)

    def test_missing_attachment_id_fails(
        self,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        job_id = setup_valid_bundle(elabftw, vm_agent)
        elabftw.patch_metadata(job_id, {"Job JSON attachment ID": {"value": ""}})

        service = ValidationService(elabftw, vm_agent)
        report = service.validate_job(job_id)
        assert report.valid is False


# --- Layout content validation (Stage 3 — Run Builder semantic rules) ---


def _make_layout(wells: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a layout dict from a list of well dicts."""
    return {
        "schema_name": "wallac.layout",
        "schema_version": 1,
        "plate_type": "96-well",
        "wells": wells,
    }


def _setup_with_layout(
    elabftw: MockValidationElabftwClient,
    vm_agent: MockVmAgentClient,
    layout_spec_dict: dict[str, Any],
) -> int:
    """Set up a valid bundle, then replace the layout with a custom one.

    Updates the layout attachment and metadata hash, AND re-canonicalizes
    the job spec to reference the new layout hash — otherwise the bundle
    would be invalid (layout drifted after job finalization).
    """
    job_id = setup_valid_bundle(elabftw, vm_agent)

    # Re-upload layout bytes + update layout metadata hash
    layout_bytes, layout_hash = canonicalize_and_hash(layout_spec_dict)
    elabftw.add_upload(11, 5002, layout_bytes)
    elabftw.patch_metadata(11, {"Layout hash": {"value": layout_hash}})

    # Re-canonicalize the job spec with the new layout hash, and update
    # the job attachment + metadata so the bundle stays consistent.
    job_spec = make_job_spec_dict(layout_hash=layout_hash)
    job_bytes, job_hash = canonicalize_and_hash(job_spec)
    elabftw.add_upload(job_id, 5004, job_bytes)
    elabftw.patch_metadata(job_id, {"Job hash": {"value": job_hash}})

    return job_id


class TestLayoutContentValidation:
    """Tests for the 9 semantic layout rules."""

    def test_valid_layout_passes_all_rules(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
                {"well_name": "B1", "role": "measured", "control_type": "blank"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        # All layout_content checks must pass
        layout_checks = [c for c in report.checks if c["name"].startswith("layout_")]
        assert len(layout_checks) >= 9, f"expected >=9 layout checks, got {len(layout_checks)}"
        for c in layout_checks:
            assert c["passed"], f"layout check failed: {c}"

    # Rule 1: empty layout (no wells)
    def test_rule1_empty_layout_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout([])
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(c["name"] == "layout_empty" and not c["passed"] for c in report.checks)

    # Rule 2: no measured wells (all skipped/excluded)
    def test_rule2_no_measured_wells_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "skipped"},
                {"well_name": "A2", "role": "excluded"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(c["name"] == "layout_no_measured" and not c["passed"] for c in report.checks)

    # Rule 3: duplicate well_name entries
    def test_rule3_duplicate_well_names_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A1", "role": "measured"},  # dup
                {"well_name": "A2", "role": "measured"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(c["name"] == "layout_duplicate_wells" and not c["passed"] for c in report.checks)

    # Rule 4: skipped well with sample_name
    def test_rule4_skipped_with_sample_name_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "skipped", "sample_name": "Sample X"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(
            c["name"] == "layout_skipped_with_sample" and not c["passed"] for c in report.checks
        )

    # Rule 5: excluded well with sample_label
    def test_rule5_excluded_with_sample_label_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded", "sample_label": "label"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(
            c["name"] == "layout_excluded_with_sample" and not c["passed"] for c in report.checks
        )

    # Rule 6: control_type set but role != measured
    def test_rule6_control_on_non_measured_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded", "control_type": "blank"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(c["name"] == "layout_control_role" and not c["passed"] for c in report.checks)

    # Rule 7: blank well that's not measured (special case of rule 6)
    def test_rule7_blank_not_measured_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "skipped", "control_type": "blank"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(
            c["name"] == "layout_blank_not_measured" and not c["passed"] for c in report.checks
        )

    # Rule 8: replicate group with only 1 well
    def test_rule8_replicate_group_with_one_well_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                # Group G1 has only 1 member — replicate implies >=2
                {"well_name": "A2", "role": "measured"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(
            c["name"] == "layout_replicate_group_size" and not c["passed"] for c in report.checks
        )

    # Rule 9: replicate group made entirely of excluded wells
    def test_rule9_replicate_group_all_excluded_fails(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        layout = _make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded", "replicate_group": "G1"},
                {"well_name": "A3", "role": "excluded", "replicate_group": "G1"},
            ]
        )
        job_id = _setup_with_layout(elabftw, vm_agent, layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        assert any(
            c["name"] == "layout_replicate_group_all_excluded" and not c["passed"]
            for c in report.checks
        )

    # Edge case: layout schema parse failure
    def test_layout_schema_parse_failure_fails_closed(
        self,
        service: ValidationService,
        elabftw: MockValidationElabftwClient,
        vm_agent: MockVmAgentClient,
    ) -> None:
        # Invalid schema_version — LayoutSpec.from_dict will raise
        bad_layout = {
            "schema_name": "wallac.layout",
            "schema_version": 999,
            "plate_type": "96-well",
            "wells": [{"well_name": "A1", "role": "measured"}],
        }
        job_id = _setup_with_layout(elabftw, vm_agent, bad_layout)
        report = service.validate_job(job_id)

        assert report.valid is False
        # layout_schema check fails (or another name depending on which error)
        # The complaint will be either parse failure or downstream effect
        assert any("layout" in c["name"] and not c["passed"] for c in report.checks)
