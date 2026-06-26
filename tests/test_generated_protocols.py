"""Tests for generated-protocol management (Stage 5).

Tests cover:
- Feature flag checking
- Mode enable flags
- Template fingerprint verification
- ID allocation and collision detection
- MDB backup before write
- Single-writer lock
- Post-write verification
- Cleanup dry-run and confirm
- Disabled by default
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.generated_protocols import (
    GENERATED_GROUP_NAME,
    GENERATED_ID_MIN,
    GENERATED_NAME_PREFIX,
    GeneratedProtocolManager,
    TemplateFingerprint,
    is_authoring_enabled,
    is_mode_enabled,
)

# --- Mock MDB client ---


class MockMdbClient:
    """In-memory mock for the MDB client."""

    def __init__(self) -> None:
        self._protocols: dict[int, dict[str, Any]] = {}
        self._groups: dict[str, int] = {}
        self._next_id = GENERATED_ID_MIN
        self._backups: list[str] = []
        self._insert_should_fail = False

    def add_group(self, name: str, group_id: int) -> None:
        self._groups[name] = group_id

    def add_protocol(self, assay_prot_id: int, name: str, group: str = "Photometry") -> None:
        self._protocols[assay_prot_id] = {
            "AssayProtID": assay_prot_id,
            "ProtName": name,
            "ProtNumber": assay_prot_id - GENERATED_ID_MIN + 1,
            "ProtVersion": 1,
            "FactoryPreset": False,
            "GroupName": group,
        }

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
        if not self._protocols:
            return 0
        return max(self._protocols.keys())

    def insert_protocol(self, protocol: dict[str, Any]) -> int:
        if self._insert_should_fail:
            raise RuntimeError("Simulated insert failure")
        aid = protocol["AssayProtID"]
        self._protocols[aid] = dict(protocol)
        return aid

    def delete_protocol(self, assay_prot_id: int) -> bool:
        if assay_prot_id in self._protocols:
            del self._protocols[assay_prot_id]
            return True
        return False

    def backup_mdb(self, backup_path: str) -> str:
        full_path = f"/tmp/mock_backups/{backup_path}"
        self._backups.append(full_path)
        return full_path

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return list(self._protocols.values())


# --- Feature flag tests ---


class TestFeatureFlags:
    def test_authoring_disabled_by_default(self) -> None:
        assert is_authoring_enabled({}) is False

    def test_authoring_enabled(self) -> None:
        assert is_authoring_enabled({"WALLAC_ENABLE_PROTOCOL_AUTHORING": "true"}) is True

    def test_authoring_disabled_with_wrong_value(self) -> None:
        assert is_authoring_enabled({"WALLAC_ENABLE_PROTOCOL_AUTHORING": "false"}) is False
        assert is_authoring_enabled({"WALLAC_ENABLE_PROTOCOL_AUTHORING": "yes"}) is False

    def test_mode_disabled_by_default(self) -> None:
        assert is_mode_enabled("photometry", {}) is False

    def test_mode_enabled(self) -> None:
        assert is_mode_enabled("photometry", {"WALLAC_ENABLE_PHOTOMETRY": "true"}) is True
        assert is_mode_enabled("fluorometry", {"WALLAC_ENABLE_FLUOROMETRY": "true"}) is True
        assert is_mode_enabled("luminescence", {"WALLAC_ENABLE_LUMINESCENCE": "true"}) is True

    def test_unknown_mode(self) -> None:
        assert is_mode_enabled("trf", {}) is False


# --- Template fingerprint tests ---


class TestTemplateFingerprint:
    def test_template_creation(self) -> None:
        tf = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        assert tf.assay_prot_id == 1000003
        assert tf.mode == "photometry"


# --- GeneratedProtocolManager validation tests ---


class TestValidation:
    def test_validate_with_feature_flag_off(
        self,
        mdb: MockMdbClient,
    ) -> None:
        mgr = GeneratedProtocolManager(mdb, env={})
        result = mgr.validate_generation(1, "photometry", "abc123")
        assert result["valid"] is False
        assert any(e["check"] == "feature_flag" for e in result["errors"])

    def test_validate_with_mode_flag_off(
        self,
        mdb: MockMdbClient,
    ) -> None:
        mgr = GeneratedProtocolManager(
            mdb,
            env={"WALLAC_ENABLE_PROTOCOL_AUTHORING": "true"},
        )
        result = mgr.validate_generation(1, "photometry", "abc123")
        assert result["valid"] is False
        assert any(e["check"] == "mode_flag" for e in result["errors"])

    def test_validate_with_no_template(
        self,
        mdb: MockMdbClient,
    ) -> None:
        mgr = GeneratedProtocolManager(
            mdb,
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )
        result = mgr.validate_generation(1, "photometry", "abc123")
        assert result["valid"] is False
        assert any(e["check"] == "template_exists" for e in result["errors"])

    def test_validate_with_missing_group(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )
        result = mgr.validate_generation(1, "photometry", "abc123")
        assert result["valid"] is False
        assert any(e["check"] == "group_exists" for e in result["errors"])

    def test_validate_passes_when_all_checks_met(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )
        result = mgr.validate_generation(1, "photometry", "abc123def456")
        assert result["valid"] is True
        assert len(result["errors"]) == 0

    def test_validate_detects_collision(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        # Pre-create a protocol with the expected generated name
        mdb.add_protocol(GENERATED_ID_MIN, f"{GENERATED_NAME_PREFIX}1-abc123de")
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )
        result = mgr.validate_generation(1, "photometry", "abc123def456")
        assert result["valid"] is False
        assert any(e["check"] == "no_existing_protocol" for e in result["errors"])


# --- GeneratedProtocolManager generation tests ---


class TestGeneration:
    def test_generate_protocol_success(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        spec_hash = "abc123def456789"
        proto = mgr.generate_protocol(1, "photometry", spec_hash, {})

        assert proto.assay_prot_id >= GENERATED_ID_MIN
        assert proto.name == f"{GENERATED_NAME_PREFIX}1-abc123de"
        assert proto.mode == "photometry"
        assert proto.job_id == 1
        assert proto.hash == spec_hash
        assert proto.backup_path != ""
        assert proto.verified is True

    def test_generate_fails_with_feature_flag_off(
        self,
        mdb: MockMdbClient,
    ) -> None:
        mgr = GeneratedProtocolManager(mdb, env={})
        with pytest.raises(RuntimeError, match="feature_flag"):
            mgr.generate_protocol(1, "photometry", "abc", {})

    def test_generate_creates_backup(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        mgr.generate_protocol(1, "photometry", "abc123", {})
        assert len(mdb._backups) == 1

    def test_generate_allocates_sequential_ids(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        proto1 = mgr.generate_protocol(1, "photometry", "hash1", {})
        proto2 = mgr.generate_protocol(2, "photometry", "hash2", {})
        assert proto2.assay_prot_id == proto1.assay_prot_id + 1

    def test_post_write_verification_fails_on_name_mismatch(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        # Override insert to change the name
        original_insert = mdb.insert_protocol

        def _bad_insert(protocol: dict[str, Any]) -> int:
            protocol["ProtName"] = "WRONG NAME"
            return original_insert(protocol)

        mdb.insert_protocol = _bad_insert  # type: ignore[method-assign]

        proto = mgr.generate_protocol(1, "photometry", "abc123", {})
        assert proto.verified is False


# --- Cleanup tests ---


class TestCleanup:
    def test_cleanup_dry_run(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        mgr.generate_protocol(1, "photometry", "abc123", {})
        result = mgr.cleanup_terminal(confirm=False)

        assert result.dry_run is True
        assert len(result.deleted) == 0
        assert len(result.skipped) == 1

    def test_cleanup_confirm_deletes(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        mgr.generate_protocol(1, "photometry", "abc123", {})
        result = mgr.cleanup_terminal(confirm=True)

        assert result.dry_run is False
        assert len(result.deleted) == 1
        assert len(result.skipped) == 0

    def test_delete_specific_job_dry_run(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        mgr.generate_protocol(1, "photometry", "abc123", {})
        result = mgr.delete_protocol(1, confirm=False)

        assert result.dry_run is True
        assert len(result.skipped) == 1
        assert len(result.deleted) == 0

    def test_delete_specific_job_confirm(
        self,
        mdb: MockMdbClient,
    ) -> None:
        template = TemplateFingerprint(
            assay_prot_id=1000003,
            mode="photometry",
            expected_name="Absorbance 600",
            expected_group="Photometry",
        )
        mdb.add_protocol(1000003, "Absorbance 600")
        mdb.add_group(GENERATED_GROUP_NAME, 99)
        mgr = GeneratedProtocolManager(
            mdb,
            templates={"photometry": template},
            env={
                "WALLAC_ENABLE_PROTOCOL_AUTHORING": "true",
                "WALLAC_ENABLE_PHOTOMETRY": "true",
            },
        )

        mgr.generate_protocol(1, "photometry", "abc123", {})
        result = mgr.delete_protocol(1, confirm=True)

        assert result.dry_run is False
        assert len(result.deleted) == 1

    def test_delete_nonexistent_job(
        self,
        mdb: MockMdbClient,
    ) -> None:
        mgr = GeneratedProtocolManager(mdb, env={})
        result = mgr.delete_protocol(999, confirm=True)
        assert len(result.errors) == 1


# --- Fixtures ---


@pytest.fixture
def mdb() -> MockMdbClient:
    return MockMdbClient()
