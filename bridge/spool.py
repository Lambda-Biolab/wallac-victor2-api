"""Filesystem spool for write-back resilience.

Implements the spool portion of Stage 6 of
docs/plans/wallac-protocol-authoring.md.

When instrument run succeeds and raw results are retrieved but eLabFTW
write-back fails, the bridge persists a local pending result package
and retries write-back later. The job is not fully completed until
eLabFTW write-back succeeds.

Spool implementation:
- Simple filesystem spool, not a new database.
- One immutable subdirectory per Automation Job/run attempt.
- Atomic temp-write/fsync/rename.
- Includes manifest, artifacts, checksums, job ID, generated AssayProtID,
  retry state.
- No eLabFTW API keys, vm-agent tokens, session tokens, or bearer tokens
  in spool.
- Permissions restricted to bridge service account and operators/admins.
- Finalized entries retained only through configured short grace period
  or moved/deleted after successful write-back.
- Pending/failed entries remain until operator resolution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default spool directory.
DEFAULT_SPOOL_DIR = "/var/lib/wallac-bridge/spool"

#: Default retention for finalized entries (seconds).
DEFAULT_GRACE_PERIOD = 86400  # 24 hours

#: Maximum retry attempts before marking as failed.
DEFAULT_MAX_RETRIES = 10


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_checksum(content: bytes) -> str:
    """Compute SHA-256 checksum for artifact content."""
    return hashlib.sha256(content).hexdigest()


@dataclass
class SpoolEntry:
    """A spooled result package for one Automation Job.

    Stored as a directory on disk with:
    - manifest.json (this dataclass serialized)
    - raw_results.json / raw_results.csv
    - analyzed_wells.csv
    - replicate_summary.csv / replicate_summary.json
    - analysis_summary.json
    """

    job_id: int
    run_id: str = ""
    assay_prot_id: int = 0
    created_at: str = ""
    retry_count: int = 0
    last_retry_at: str = ""
    status: str = "pending"  # "pending", "writing", "completed", "failed"
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    analysis_summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "assay_prot_id": self.assay_prot_id,
            "created_at": self.created_at,
            "retry_count": self.retry_count,
            "last_retry_at": self.last_retry_at,
            "status": self.status,
            "artifacts": list(self.artifacts),
            "analysis_summary": dict(self.analysis_summary),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SpoolEntry:
        return cls(
            job_id=int(d["job_id"]),
            run_id=str(d.get("run_id", "")),
            assay_prot_id=int(d.get("assay_prot_id", 0)),
            created_at=str(d.get("created_at", "")),
            retry_count=int(d.get("retry_count", 0)),
            last_retry_at=str(d.get("last_retry_at", "")),
            status=str(d.get("status", "pending")),
            artifacts=list(d.get("artifacts", [])),
            analysis_summary=dict(d.get("analysis_summary", {})),
            error=str(d.get("error", "")),
        )


class ResultSpool:
    """Filesystem spool for write-back resilience.

    Each Automation Job gets one immutable subdirectory under the spool
    root. The directory contains a manifest.json and artifact files.

    All writes are atomic: temp-write → fsync → rename.
    """

    def __init__(
        self,
        spool_dir: str = DEFAULT_SPOOL_DIR,
        grace_period: int = DEFAULT_GRACE_PERIOD,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.root = Path(spool_dir)
        self.grace_period = grace_period
        self.max_retries = max_retries
        self.root.mkdir(parents=True, exist_ok=True)

    def _entry_dir(self, job_id: int) -> Path:
        """Return the spool directory for a job."""
        return self.root / f"job-{job_id}"

    def _atomic_write(self, path: Path, content: bytes) -> None:
        """Write content atomically: temp → fsync → rename."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(path)

    def spool_results(
        self,
        job_id: int,
        run_id: str,
        assay_prot_id: int,
        artifacts: list[dict[str, Any]],
        analysis_summary: dict[str, Any],
    ) -> SpoolEntry:
        """Persist a result package to the spool.

        Args:
            job_id: Automation Job item ID.
            run_id: vm-agent run ID.
            assay_prot_id: Generated MDB protocol ID (if applicable).
            artifacts: List of {filename, content, checksum} dicts.
            analysis_summary: Analysis summary dict.

        Returns:
            The created SpoolEntry.
        """
        entry_dir = self._entry_dir(job_id)
        entry_dir.mkdir(parents=True, exist_ok=True)

        entry = SpoolEntry(
            job_id=job_id,
            run_id=run_id,
            assay_prot_id=assay_prot_id,
            created_at=now_iso(),
            status="pending",
            artifacts=[],
            analysis_summary=analysis_summary,
        )

        # Write artifacts
        for artifact in artifacts:
            filename = artifact["filename"]
            content = artifact["content"]
            checksum = artifact.get("checksum") or compute_checksum(content)

            artifact_path = entry_dir / filename
            self._atomic_write(artifact_path, content)

            entry.artifacts.append(
                {
                    "filename": filename,
                    "checksum": checksum,
                    "size": len(content),
                }
            )

        # Write manifest
        manifest_path = entry_dir / "manifest.json"
        self._atomic_write(manifest_path, json.dumps(entry.to_dict(), sort_keys=True).encode())

        logger.info("Spooled results for job %d to %s", job_id, entry_dir)
        return entry

    def load_entry(self, job_id: int) -> SpoolEntry | None:
        """Load the spool entry for a job. Returns None if not found."""
        manifest_path = self._entry_dir(job_id) / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path) as f:
                return SpoolEntry.from_dict(json.loads(f.read()))
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to load spool entry for job %d: %s", job_id, e)
            return None

    def read_artifact(self, job_id: int, filename: str) -> bytes | None:
        """Read an artifact file from the spool. Returns None if not found."""
        path = self._entry_dir(job_id) / filename
        if not path.exists():
            return None
        return path.read_bytes()

    def list_pending(self) -> list[SpoolEntry]:
        """List all pending/failed spool entries."""
        entries = []
        for entry_dir in sorted(self.root.iterdir()):
            if not entry_dir.is_dir():
                continue
            manifest = entry_dir / "manifest.json"
            if not manifest.exists():
                continue
            try:
                with open(manifest) as f:
                    entry = SpoolEntry.from_dict(json.loads(f.read()))
                if entry.status in ("pending", "failed"):
                    entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue
        return entries

    def mark_writing(self, job_id: int) -> None:
        """Mark an entry as currently being written back."""
        self._update_status(job_id, "writing")

    def mark_completed(self, job_id: int) -> None:
        """Mark an entry as successfully written back."""
        self._update_status(job_id, "completed")

    def mark_failed(self, job_id: int, error: str) -> None:
        """Mark an entry as failed after max retries."""
        self._update_status(job_id, "failed", error=error)

    def increment_retry(self, job_id: int) -> int:
        """Increment the retry count. Returns the new count."""
        entry = self.load_entry(job_id)
        if entry is None:
            return 0
        entry.retry_count += 1
        entry.last_retry_at = now_iso()
        if entry.retry_count >= self.max_retries:
            entry.status = "failed"
            entry.error = f"Max retries ({self.max_retries}) exceeded"
        self._write_manifest(job_id, entry)
        return entry.retry_count

    def cleanup_completed(self, grace_period: int | None = None) -> list[int]:
        """Remove completed entries older than the grace period.

        Returns list of job IDs that were cleaned up.
        """
        period = grace_period if grace_period is not None else self.grace_period
        now = datetime.now(timezone.utc)
        cleaned = []

        for entry_dir in sorted(self.root.iterdir()):
            if not entry_dir.is_dir():
                continue
            job_id = self._cleanup_if_eligible(entry_dir, period, now)
            if job_id is not None:
                cleaned.append(job_id)

        return cleaned

    def _cleanup_if_eligible(self, entry_dir: Path, period: int, now: datetime) -> int | None:
        """Check if a spool entry is eligible for cleanup. Returns job_id or None."""
        manifest = entry_dir / "manifest.json"
        if not manifest.exists():
            return None
        try:
            with open(manifest) as f:
                entry = SpoolEntry.from_dict(json.loads(f.read()))
        except (json.JSONDecodeError, KeyError):
            return None

        if entry.status != "completed" or not entry.created_at:
            return None

        try:
            created = datetime.fromisoformat(entry.created_at)
        except (ValueError, TypeError):
            return None

        if (now - created).total_seconds() <= period:
            return None

        import shutil

        shutil.rmtree(entry_dir)
        return entry.job_id

    def _update_status(self, job_id: int, status: str, error: str = "") -> None:
        entry = self.load_entry(job_id)
        if entry is None:
            return
        entry.status = status
        if error:
            entry.error = error
        self._write_manifest(job_id, entry)

    def _write_manifest(self, job_id: int, entry: SpoolEntry) -> None:
        manifest_path = self._entry_dir(job_id) / "manifest.json"
        self._atomic_write(
            manifest_path,
            json.dumps(entry.to_dict(), sort_keys=True).encode(),
        )

    def scan_for_secrets(self) -> list[str]:
        """Scan spool contents for potential secrets. Returns list of findings.

        Checks for common secret patterns in artifact filenames and content.
        No secrets should ever be in the spool, but this is a safety check.
        """
        findings: list[str] = []
        for entry_dir in self.root.iterdir():
            if not entry_dir.is_dir():
                continue
            for f in entry_dir.iterdir():
                if not f.is_file():
                    continue
                self._scan_file_for_secrets(f, findings)
        return findings

    def _scan_file_for_secrets(self, f: Path, findings: list[str]) -> None:
        """Scan a single file for secret patterns."""
        secret_patterns = (
            "api_key",
            "apikey",
            "api-key",
            "bearer",
            "token",
            "secret",
            "password",
            "passwd",
            "credential",
        )
        name_lower = f.name.lower()
        for pattern in secret_patterns:
            if pattern in name_lower:
                findings.append(f"{f}: filename contains '{pattern}'")
        try:
            content = f.read_bytes()[:1024].decode("utf-8", errors="ignore").lower()
            for pattern in secret_patterns:
                if pattern in content:
                    findings.append(f"{f}: content contains '{pattern}'")
                    break
        except Exception:
            pass
