"""eLabFTW write-back for the Wallac bridge.

Implements issue #4: bridge-side eLabFTW write-back from runtime state and
terminal result packages.

Write-back is the bridge's mechanism for projecting runtime state back into
eLabFTW (the durable provenance surface).  It covers:

  - **Claim fields**: claimed_by, claimed_at, last_heartbeat, Wallac run ID,
    device identity, Live Monitor URL.
  - **Throttled progress snapshots**: not every telemetry event, but periodic
    state/progress updates.
  - **Terminal result packages**: final state, result summary, event log,
    artifact manifest, errors, and operator hints.
  - **Artifact uploads**: CSV/grid/raw result files uploaded to eLabFTW and
    linked from the Automation Job.
  - **Retry**: write-back is retried from preserved local state after
    transient eLabFTW/network failures.

Write-back is **append-only where possible** — it never mutates signed
request fields (protocol, plate layout, expected outputs, etc.).

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
                 eLabFTW-lambdabiolab/docs/automation-integrations.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .elabftw import ElabftwInterface
from .errors import BridgeError
from .models import FinalState, JobState

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_checksum(content: bytes) -> str:
    """Compute a SHA-256 checksum for an artifact."""
    return hashlib.sha256(content).hexdigest()


# --- Throttling ------------------------------------------------------------


# Minimum time between progress writes (seconds).
PROGRESS_WRITE_INTERVAL_SECONDS = 30.0
# Minimum progress delta to trigger a write (percentage points).
PROGRESS_WRITE_DELTA_PERCENT = 5.0


class ProgressTracker:
    """Throttles progress write-backs to avoid flooding eLabFTW.

    A progress write is allowed when:
      - The state changed (always write on state transitions).
      - The progress percent changed by >= ``delta_percent`` since last write.
      - ``interval_seconds`` have passed since the last write.
      - The caller forces a write (e.g., terminal state).
    """

    def __init__(
        self,
        interval_seconds: float = PROGRESS_WRITE_INTERVAL_SECONDS,
        delta_percent: float = PROGRESS_WRITE_DELTA_PERCENT,
    ) -> None:
        self._interval = interval_seconds
        self._delta = delta_percent
        self._last_written_percent: float | None = None
        self._last_written_state: str | None = None
        self._last_written_time: float = 0.0

    def should_write(self, percent: float, state: str, *, force: bool = False) -> bool:
        """Return True if a progress write should be performed now."""
        if force:
            return True

        now = time.monotonic()

        # Always write on state change
        if state != self._last_written_state:
            return True

        # Write if enough time has passed
        if now - self._last_written_time >= self._interval:
            return True

        # Write if progress changed enough
        if self._last_written_percent is None:
            return True
        return abs(percent - self._last_written_percent) >= self._delta

    def mark_written(self, percent: float, state: str) -> None:
        """Record that a progress write was performed."""
        self._last_written_percent = percent
        self._last_written_state = state
        self._last_written_time = time.monotonic()

    def reset(self) -> None:
        """Reset tracking state (e.g., for a new job)."""
        self._last_written_percent = None
        self._last_written_state = None
        self._last_written_time = 0.0


# --- Terminal result package ------------------------------------------------


@dataclass
class ArtifactEntry:
    """A single result artifact with its checksum."""

    filename: str
    content: bytes
    comment: str = ""
    checksum: str = ""
    upload_id: int | None = None  # set after upload

    def __post_init__(self) -> None:
        if not self.checksum:
            self.checksum = compute_checksum(self.content)


@dataclass
class ResultPackage:
    """Terminal result package for a completed/failed/aborted job.

    Follows the terminal result package spec from automation-integrations.md.
    """

    job_id: int
    experiment_id: str = ""
    protocol_name: str = ""
    final_state: FinalState = FinalState.COMPLETED
    started_at: str = ""
    ended_at: str = ""
    requester: str = ""
    operator: str = ""
    service_identity: str = ""
    device_identity: str = ""
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    validation_summary: str = ""
    event_log: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    result_summary: str = ""
    operator_hint: str = ""

    def to_manifest(self) -> dict[str, Any]:
        """Build an artifact manifest dict for eLabFTW write-back."""
        return {
            "artifacts": [
                {
                    "filename": a.filename,
                    "checksum": a.checksum,
                    "size": len(a.content),
                    "upload_id": a.upload_id,
                }
                for a in self.artifacts
            ]
        }


# --- Write-back manager -----------------------------------------------------


class WriteBackManager:
    """Orchestrates all eLabFTW write-back for a single Automation Job.

    Handles claim fields, throttled progress, terminal result packages,
    artifact uploads, and retry on transient failures.
    """

    def __init__(
        self,
        client: ElabftwInterface,
        bridge_identity: str,
        device_identity: str = "",
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.bridge_identity = bridge_identity
        self.device_identity = device_identity
        self.max_retries = max_retries
        self.retry_delay = retry_delay_seconds
        self.progress = ProgressTracker()

    def _retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Retry a callable on transient failures.

        Retries up to ``max_retries`` times with ``retry_delay`` between
        attempts.  Non-transient errors (BridgeError) are re-raised
        immediately.
        """
        import time as _time

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except BridgeError:
                raise  # non-transient, don't retry
            except (ConnectionError, OSError, TimeoutError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    logger.warning(
                        "Write-back attempt %d/%d failed: %s, retrying in %ss",
                        attempt + 1,
                        self.max_retries + 1,
                        e,
                        self.retry_delay,
                    )
                    _time.sleep(self.retry_delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    # --- Claim fields ------------------------------------------------------

    def write_claim(
        self,
        item_id: int,
        *,
        wallac_run_id: str = "",
        live_monitor_url: str = "",
    ) -> None:
        """Write claim fields after a job is claimed.

        Sets: claimed_by, claimed_at, last_heartbeat, Wallac run ID,
        device identity, Live Monitor URL.
        """
        self._retry(
            self.client.patch_metadata,
            item_id,
            {
                "Claimed by": {
                    "type": "text",
                    "value": self.bridge_identity,
                },
                "Claimed at": {
                    "type": "datetime-local",
                    "value": now_iso(),
                },
                "Last heartbeat": {
                    "type": "datetime-local",
                    "value": now_iso(),
                },
                "Wallac run ID": {
                    "type": "text",
                    "value": wallac_run_id,
                },
                "Device identity": {
                    "type": "text",
                    "value": self.device_identity,
                },
                "Live Monitor": {
                    "type": "url",
                    "value": live_monitor_url,
                },
            },
        )

    # --- Heartbeat ---------------------------------------------------------

    def write_heartbeat(self, item_id: int) -> None:
        """Write a heartbeat timestamp to eLabFTW."""
        self._retry(
            self.client.patch_metadata,
            item_id,
            {
                "Last heartbeat": {
                    "type": "datetime-local",
                    "value": now_iso(),
                },
            },
        )

    # --- Progress ----------------------------------------------------------

    def write_progress(
        self,
        item_id: int,
        percent: float,
        step: str,
        state: str,
        *,
        force: bool = False,
    ) -> bool:
        """Write a throttled progress snapshot to eLabFTW.

        Returns True if a write was performed, False if throttled.
        """
        if not self.progress.should_write(percent, state, force=force):
            return False

        self._retry(
            self.client.patch_metadata,
            item_id,
            {
                "Automation state": {
                    "type": "select",
                    "value": state,
                },
                "Progress percent": {
                    "type": "number",
                    "value": percent,
                },
                "Current step": {
                    "type": "text",
                    "value": step,
                },
                "Last heartbeat": {
                    "type": "datetime-local",
                    "value": now_iso(),
                },
            },
        )
        self.progress.mark_written(percent, state)
        return True

    # --- Event log ---------------------------------------------------------

    def write_event(self, item_id: int, event: str) -> None:
        """Append an event to the job's comment thread (event log).

        Uses eLabFTW comments as an append-only event log.
        """
        timestamped = f"[{now_iso()}] {event}"
        self._retry(self.client.post_comment, item_id, timestamped)

    # --- Artifact upload ---------------------------------------------------

    def upload_artifact(
        self,
        item_id: int,
        filename: str,
        content: bytes,
        comment: str = "",
    ) -> ArtifactEntry:
        """Upload a result artifact to eLabFTW and return an ArtifactEntry."""
        entry = ArtifactEntry(filename=filename, content=content, comment=comment)

        upload = self._retry(
            self.client.upload_file,
            item_id,
            filename,
            content,
            f"{comment} (sha256:{entry.checksum})" if comment else f"sha256:{entry.checksum}",
        )
        entry.upload_id = upload.get("id")
        return entry

    # --- Terminal result package -------------------------------------------

    def write_terminal(self, item_id: int, package: ResultPackage) -> None:
        """Write the terminal result package to eLabFTW.

        This is the final write-back for a job.  It:
          1. Uploads all artifacts.
          2. Writes the final state, result summary, artifact manifest,
             errors, and operator hints to metadata.
          3. Appends the event log as comments.
          4. Forces a final progress write (100% or current).
        """
        # 1. Upload artifacts
        for artifact in package.artifacts:
            if artifact.upload_id is None:
                uploaded = self.upload_artifact(
                    item_id,
                    artifact.filename,
                    artifact.content,
                    artifact.comment,
                )
                artifact.upload_id = uploaded.upload_id

        # 2. Build the metadata update
        manifest = package.to_manifest()
        percent = 100.0 if package.final_state == FinalState.COMPLETED else 0.0

        # Map FinalState to JobState for the Automation state field
        state_map = {
            FinalState.COMPLETED: JobState.COMPLETED.value,
            FinalState.FAILED: JobState.FAILED.value,
            FinalState.ABORTED: JobState.ABORTED.value,
            FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW: (
                JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value
            ),
        }

        # Build error summary
        error_summary = ""
        if package.errors:
            first_error = package.errors[0]
            error_summary = first_error.get("code", "error")

        metadata = {
            "Automation state": {
                "type": "select",
                "value": state_map.get(
                    package.final_state,
                    JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value,
                ),
            },
            "Final state": {
                "type": "select",
                "value": package.final_state.value,
            },
            "Progress percent": {
                "type": "number",
                "value": percent,
            },
            "Current step": {
                "type": "text",
                "value": package.final_state.value,
            },
            "Result summary": {
                "type": "text",
                "value": package.result_summary,
            },
            "Artifact manifest": {
                "type": "url",
                "value": json.dumps(manifest, ensure_ascii=False),
            },
            "Last error code": {
                "type": "text",
                "value": error_summary,
            },
            "Operator hint": {
                "type": "text",
                "value": package.operator_hint,
            },
            "Last heartbeat": {
                "type": "datetime-local",
                "value": now_iso(),
            },
        }

        self._retry(self.client.patch_metadata, item_id, metadata)

        # 3. Append event log as comments
        for event in package.event_log:
            self._retry(self.client.post_comment, item_id, event)

        # 4. Force final progress tracking
        self.progress.mark_written(percent, state_map[package.final_state])

        logger.info(
            "Job %d: terminal write-back complete (final_state=%s, artifacts=%d, events=%d)",
            item_id,
            package.final_state.value,
            len(package.artifacts),
            len(package.event_log),
        )
