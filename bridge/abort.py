"""Abort detection for the Wallac bridge.

Implements the abort detection paths from issue #5:

  1. **eLabFTW abort polling** — detect ``Requested action = abort`` on
     claimed/running jobs within ~5-15 seconds.  This is non-real-time
     operator cancel intent, not an emergency stop.

  2. **Dashboard abort** — lower-latency direct abort from the live
     dashboard.  The dashboard calls the bridge runtime directly, which
     immediately signals the state machine.

Emergency stops are a separate physical path and are NOT handled here.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
"""

from __future__ import annotations

import logging
from typing import Any

from .elabftw import ElabftwInterface
from .lifecycle import AbortSource, LifecycleManager
from .models import JobState

logger = logging.getLogger(__name__)

# Poll eLabFTW for abort requests at this interval (seconds).
# The contract requires detection within ~5-15 seconds; 5s polling gives
# a worst-case detection latency of ~5s plus network RTT.
ABORT_POLL_INTERVAL_SECONDS = 5.0


class AbortDetector:
    """Detects abort requests from eLabFTW polling.

    The bridge's main loop calls :meth:`check_for_aborts` periodically
    (every ``ABORT_POLL_INTERVAL_SECONDS``).  For each running job, it
    re-reads the ``Requested action`` field from eLabFTW.  If the operator
    changed it to ``abort``, the detector signals the job's
    :class:`LifecycleManager`.
    """

    def __init__(self, client: ElabftwInterface) -> None:
        self.client = client

    def check_for_aborts(
        self, active_jobs: list[tuple[int, LifecycleManager]]
    ) -> list[dict[str, Any]]:
        """Check all active jobs for eLabFTW abort requests.

        Args:
            active_jobs: List of (item_id, lifecycle_manager) for jobs
                         that are currently running or abort-requested.

        Returns:
            List of result dicts for jobs where an abort was detected.
        """
        results: list[dict[str, Any]] = []

        for item_id, lifecycle in active_jobs:
            # Only check jobs that are running or already abort-requested
            if lifecycle.state not in (JobState.RUNNING, JobState.ABORT_REQUESTED):
                continue

            try:
                action = self._read_requested_action(item_id)
            except Exception:
                logger.exception("Failed to read requested action for job %d", item_id)
                continue

            if action == "abort" and lifecycle.state == JobState.RUNNING:
                lifecycle.request_abort(AbortSource.ELABFTW)
                results.append(
                    {
                        "item_id": item_id,
                        "abort_source": AbortSource.ELABFTW.value,
                        "new_state": lifecycle.state.value,
                    }
                )
                logger.info("Job %d: eLabFTW abort detected", item_id)

        return results

    def _read_requested_action(self, item_id: int) -> str:
        """Re-read the Requested action field from eLabFTW for a job."""
        jobs = self.client.list_automation_jobs()
        job = next((j for j in jobs if j.item_id == item_id), None)
        if job is None:
            return "none"
        return job.request_fields.requested_action


class DashboardAbortHandler:
    """Handles direct abort requests from the live dashboard.

    The dashboard calls :meth:`request_abort` which immediately signals
    the job's :class:`LifecycleManager`.  This is lower-latency than
    waiting for the eLabFTW polling cycle.
    """

    def __init__(self) -> None:
        self._active: dict[int, LifecycleManager] = {}

    def register(self, item_id: int, lifecycle: LifecycleManager) -> None:
        """Register an active job for dashboard abort handling."""
        self._active[item_id] = lifecycle

    def unregister(self, item_id: int) -> None:
        """Remove a job after it reaches a terminal state."""
        self._active.pop(item_id, None)

    def request_abort(self, item_id: int) -> dict[str, Any]:
        """Request a controlled abort from the dashboard.

        Returns a result dict with the new state.

        Raises ``KeyError`` if the job is not registered (not active).
        """
        lifecycle = self._active[item_id]
        old_state = lifecycle.state.value
        lifecycle.request_abort(AbortSource.DASHBOARD)
        return {
            "item_id": item_id,
            "abort_source": AbortSource.DASHBOARD.value,
            "old_state": old_state,
            "new_state": lifecycle.state.value,
        }

    def is_registered(self, item_id: int) -> bool:
        return item_id in self._active
