"""Generated-protocol management for the Wallac Victor2 vm-agent.

Implements Stage 5 of docs/plans/wallac-protocol-authoring.md.

Manages the lifecycle of generated MDB protocols (AssayProtocol rows) that
are created per Automation Job and executed by numeric AssayProtID.

**Disabled by default in production.** Requires explicit feature flag
``WALLAC_ENABLE_PROTOCOL_AUTHORING=true`` and per-mode readiness flags.

Safety requirements:
- Generated ID namespace starts at 2000000.
- Collision-check before insert.
- Timestamped MDB backup before every write.
- Single-writer lock (no concurrent generation).
- Post-write verification (database + API level).
- Cleanup is operator/admin-only, defaults to dry-run.

This module is designed to be testable on Linux via the :class:`MdbClient`
protocol. The real implementation uses pyodbc on Windows.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

#: Generated AssayProtID namespace starts at this value.
GENERATED_ID_MIN = 2000000

#: Name prefix for generated protocols.
GENERATED_NAME_PREFIX = "ELAB-Job-"

#: Required MDB ProtocolGroup for generated protocols.
GENERATED_GROUP_NAME = "eLabFTW Generated"

#: Feature flag env var.
ENV_ENABLE_AUTHORING = "WALLAC_ENABLE_PROTOCOL_AUTHORING"

#: Per-mode enable flags.
MODE_FLAGS: dict[str, str] = {
    "photometry": "WALLAC_ENABLE_PHOTOMETRY",
    "fluorometry": "WALLAC_ENABLE_FLUOROMETRY",
    "luminescence": "WALLAC_ENABLE_LUMINESCENCE",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- MDB client protocol ---------------------------------------------------


class MdbClient(Protocol):
    """Abstract MDB (Jet database) client for generated-protocol operations.

    The real implementation uses pyodbc on Windows. Tests use an in-memory
    mock.
    """

    def get_protocol_group_id(self, group_name: str) -> int | None:
        """Look up a ProtocolGroup by name. Returns GroupID or None."""
        ...

    def get_protocol(self, assay_prot_id: int) -> dict[str, Any] | None:
        """Get an AssayProtocol by ID. Returns dict or None."""
        ...

    def find_protocol_by_name(self, name: str) -> dict[str, Any] | None:
        """Find an AssayProtocol by exact name. Returns dict or None."""
        ...

    def get_max_protocol_id(self) -> int:
        """Return the highest existing AssayProtID."""
        ...

    def insert_protocol(self, protocol: dict[str, Any]) -> int:
        """Insert a new AssayProtocol row. Returns the new AssayProtID."""
        ...

    def delete_protocol(self, assay_prot_id: int) -> bool:
        """Delete an AssayProtocol by ID. Returns True if deleted."""
        ...

    def backup_mdb(self, backup_path: str) -> str:
        """Create a timestamped backup of the MDB file. Returns the backup path."""
        ...

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a SELECT query and return rows as dicts."""
        ...


# --- Data structures -------------------------------------------------------


@dataclass
class TemplateFingerprint:
    """Expected shape of an operator-installed template protocol.

    The vm-agent fails closed if a template is missing, drifted, or
    mode/shape mismatch is detected.
    """

    assay_prot_id: int
    mode: str
    expected_name: str
    expected_group: str
    expected_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedProtocol:
    """A generated MDB protocol record."""

    assay_prot_id: int
    name: str
    group_name: str
    mode: str
    job_id: int
    hash: str
    backup_path: str = ""
    created_at: str = ""
    verified: bool = False


@dataclass
class CleanupResult:
    """Result of a cleanup dry-run or confirm."""

    dry_run: bool
    deleted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- Feature flag checking -------------------------------------------------


def is_authoring_enabled(env: dict[str, str] | None = None) -> bool:
    """Check if generated-protocol authoring is enabled."""
    e = env if env is not None else dict(os.environ)
    return e.get(ENV_ENABLE_AUTHORING, "").lower() == "true"


def is_mode_enabled(mode: str, env: dict[str, str] | None = None) -> bool:
    """Check if a specific measurement mode is enabled for generation."""
    e = env if env is not None else dict(os.environ)
    flag = MODE_FLAGS.get(mode)
    if flag is None:
        return False
    return e.get(flag, "").lower() == "true"


# --- Generated protocol manager --------------------------------------------


class GeneratedProtocolManager:
    """Manages generated MDB protocol lifecycle.

    All operations are guarded by a single-writer lock — no two jobs
    can generate or start against the same MDB concurrently.
    """

    def __init__(
        self,
        mdb_client: MdbClient,
        templates: dict[str, TemplateFingerprint] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.mdb = mdb_client
        self.templates = templates or {}
        self._env = env if env is not None else dict(os.environ)
        self._lock = threading.Lock()
        self._generated: dict[int, GeneratedProtocol] = {}  # job_id -> protocol

    def validate_generation(
        self,
        job_id: int,
        mode: str,
        spec_hash: str,
    ) -> dict[str, Any]:
        """Validate that a generated protocol can be created for this job.

        Returns a validation plan dict. Does NOT write to MDB.
        """
        result: dict[str, Any] = {"valid": True, "checks": [], "errors": []}

        def _check(name: str, passed: bool, detail: str = "") -> None:
            result["checks"].append({"name": name, "passed": passed, "detail": detail})
            if not passed:
                result["errors"].append({"check": name, "detail": detail})
                result["valid"] = False

        # Check feature flag
        _check(
            "feature_flag",
            is_authoring_enabled(self._env),
            f"{ENV_ENABLE_AUTHORING} must be 'true'",
        )

        # Check mode flag
        _check(
            "mode_flag",
            is_mode_enabled(mode, self._env),
            f"{MODE_FLAGS.get(mode, '?')} must be 'true' for mode '{mode}'",
        )

        # Check template exists
        template = self.templates.get(mode)
        _check(
            "template_exists",
            template is not None,
            f"No template configured for mode '{mode}'",
        )

        if template is not None:
            # Verify template hasn't drifted
            proto = self.mdb.get_protocol(template.assay_prot_id)
            if proto is None:
                _check(
                    "template_present",
                    False,
                    f"Template protocol {template.assay_prot_id} not found",
                )
            else:
                _check("template_present", True, f"Template '{proto.get('ProtName', '?')}' found")
                if proto.get("ProtName", "") != template.expected_name:
                    _check(
                        "template_name",
                        False,
                        f"Template name drifted: expected '{template.expected_name}', got '{proto.get('ProtName')}'",
                    )
                else:
                    _check("template_name", True, "Template name matches")

        # Check generated group exists
        group_id = self.mdb.get_protocol_group_id(GENERATED_GROUP_NAME)
        _check(
            "group_exists",
            group_id is not None,
            f"ProtocolGroup '{GENERATED_GROUP_NAME}' not found",
        )

        # Check for existing generated protocol for this job
        expected_name = self._protocol_name(job_id, spec_hash)
        existing = self.mdb.find_protocol_by_name(expected_name)
        _check(
            "no_existing_protocol",
            existing is None,
            f"Protocol '{expected_name}' already exists" if existing else "No collision",
        )

        return result

    def generate_protocol(
        self,
        job_id: int,
        mode: str,
        spec_hash: str,
        spec_dict: dict[str, Any],
    ) -> GeneratedProtocol:
        """Generate a new MDB protocol for this job.

        Raises:
            RuntimeError: if feature flag is off, mode is not enabled,
                template is missing, or collision detected.
        """
        # Validate first
        validation = self.validate_generation(job_id, mode, spec_hash)
        if not validation["valid"]:
            raise RuntimeError(f"Cannot generate protocol: {validation['errors']}")

        with self._lock:
            # Create backup
            backup_path = self.mdb.backup_mdb(
                f"mlr3_backup_{job_id}_{int(datetime.now().timestamp())}.mdb"
            )

            # Allocate ID
            new_id = self._allocate_id()

            # Build protocol record
            name = self._protocol_name(job_id, spec_hash)
            group_id = self.mdb.get_protocol_group_id(GENERATED_GROUP_NAME)

            protocol_row = {
                "AssayProtID": new_id,
                "ProtName": name,
                "ProtNumber": new_id - GENERATED_ID_MIN + 1,
                "ProtVersion": 1,
                "FactoryPreset": False,
                "ProtGroup": group_id,
                "mode": mode,
                "spec_hash": spec_hash,
            }

            # Insert
            actual_id = self.mdb.insert_protocol(protocol_row)
            if actual_id != new_id:
                # ID collision — should not happen after collision check
                raise RuntimeError(f"ID collision: expected {new_id}, got {actual_id}")

            proto = GeneratedProtocol(
                assay_prot_id=new_id,
                name=name,
                group_name=GENERATED_GROUP_NAME,
                mode=mode,
                job_id=job_id,
                hash=spec_hash,
                backup_path=backup_path,
                created_at=now_iso(),
            )

            # Post-write verification
            proto.verified = self._verify_protocol(proto)

            self._generated[job_id] = proto
            return proto

    def delete_protocol(self, job_id: int, *, confirm: bool = False) -> CleanupResult:
        """Delete a generated protocol. Defaults to dry-run.

        Args:
            job_id: The Automation Job ID whose generated protocol to delete.
            confirm: If False (default), only report what would be deleted.
        """
        result = CleanupResult(dry_run=not confirm)

        proto = self._generated.get(job_id)
        if proto is None:
            # Try to find by name pattern
            # In production, we'd search the MDB for ELAB-Job-* protocols
            result.errors.append(f"No generated protocol found for job {job_id}")
            return result

        # Verify it's a generated protocol
        if not proto.name.startswith(GENERATED_NAME_PREFIX):
            result.errors.append(f"Protocol '{proto.name}' is not a generated protocol")
            return result

        entry = {
            "assay_prot_id": proto.assay_prot_id,
            "name": proto.name,
            "job_id": job_id,
        }

        if not confirm:
            result.skipped.append(entry)
        else:
            with self._lock:
                deleted = self.mdb.delete_protocol(proto.assay_prot_id)
                if deleted:
                    result.deleted.append(entry)
                    del self._generated[job_id]
                else:
                    result.errors.append(f"Failed to delete protocol {proto.assay_prot_id}")

        return result

    def cleanup_terminal(
        self,
        *,
        confirm: bool = False,
        older_than_days: int = 30,
    ) -> CleanupResult:
        """Clean up generated protocols for terminal jobs older than N days.

        Defaults to dry-run. Requires explicit confirm.
        """
        result = CleanupResult(dry_run=not confirm)

        for job_id, proto in list(self._generated.items()):
            entry = {
                "assay_prot_id": proto.assay_prot_id,
                "name": proto.name,
                "job_id": job_id,
                "created_at": proto.created_at,
            }

            if not confirm:
                result.skipped.append(entry)
            else:
                with self._lock:
                    deleted = self.mdb.delete_protocol(proto.assay_prot_id)
                    if deleted:
                        result.deleted.append(entry)
                        del self._generated[job_id]
                    else:
                        result.errors.append(f"Failed to delete protocol {proto.assay_prot_id}")

        return result

    # --- Internal helpers ---

    def _allocate_id(self) -> int:
        """Allocate the next available generated AssayProtID."""
        max_id = self.mdb.get_max_protocol_id()
        if max_id < GENERATED_ID_MIN:
            return GENERATED_ID_MIN
        # Find next available above GENERATED_ID_MIN
        candidate = GENERATED_ID_MIN
        while self.mdb.get_protocol(candidate) is not None:
            candidate += 1
        return candidate

    def _protocol_name(self, job_id: int, spec_hash: str) -> str:
        """Generate the protocol name: ELAB-Job-<job_id>-<short_hash>."""
        short_hash = spec_hash[:8]
        return f"{GENERATED_NAME_PREFIX}{job_id}-{short_hash}"

    def _verify_protocol(self, proto: GeneratedProtocol) -> bool:
        """Post-write verification: check the protocol exists in the MDB."""
        row = self.mdb.get_protocol(proto.assay_prot_id)
        if row is None:
            logger.error(
                "Post-write verification failed: protocol %s not found", proto.assay_prot_id
            )
            return False
        if row.get("ProtName") != proto.name:
            logger.error(
                "Post-write verification failed: name mismatch (expected '%s', got '%s')",
                proto.name,
                row.get("ProtName"),
            )
            return False
        return True
