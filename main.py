#!/usr/bin/env python3
"""Wallac Victor2 bridge daemon.

Polls eLabFTW for Automation Jobs, claims them, executes them on the
Wallac Victor2 plate reader via the vm-agent, and writes results back.

Runs as a systemd service on the Linux host (lambdabiolab-computer).
The dashboard server runs in a background thread. An abort poller thread
checks eLabFTW for abort requests every 5 seconds.

Usage::

    python3 main.py

Configuration is via environment variables (see bridge/config.py).
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Any

from bridge.abort import AbortDetector
from bridge.config import BridgeConfig
from bridge.dashboard import DashboardServer, DashboardStateStore
from bridge.elabftw import get_field_value
from bridge.factory import create_elabftw_client, create_intake, create_orchestrator
from bridge.models import JobState
from bridge.vm_agent_client import VmAgentClient, VmAgentError

logger = logging.getLogger("wallac-bridge")

ABORT_POLL_INTERVAL = 5.0


class BridgeDaemon:
    """Main daemon: polls eLabFTW, executes jobs, writes back results.

    The daemon runs three concurrent activities:
    1. Main poll loop (main thread): intake → execute → writeback
    2. Abort poller (background thread): checks eLabFTW for abort requests
    3. Dashboard server (background thread): serves the live dashboard
    """

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.elabftw = create_elabftw_client(config)
        self.intake = create_intake(config)
        self.orchestrator = create_orchestrator(config)
        self.vm_agent = VmAgentClient(
            base_url=config.vm_agent_url,
            token=config.vm_agent_token,
        )

        # Abort detection
        self.abort_detector = AbortDetector(self.elabftw)
        self._active_runs: dict[int, str] = {}  # item_id -> run_id
        self._lock = threading.Lock()

        # Dashboard
        self.state_store = DashboardStateStore()
        self.dashboard = DashboardServer(
            state_store=self.state_store,
            host=config.dashboard_host,
            port=config.dashboard_port,
            session_token=config.dashboard_token or None,
        )

        self._running = False

    def run(self) -> None:
        """Main daemon loop. Blocks until stopped."""
        self._running = True
        self.dashboard.start()

        # Start abort poller in background
        abort_thread = threading.Thread(target=self._abort_loop, daemon=True)
        abort_thread.start()

        # Drain spool on startup
        self._drain_spool()

        logger.info(
            "Bridge daemon started (identity=%s, eLabFTW=%s, vm-agent=%s, poll=%ss)",
            self.config.bridge_identity,
            self.config.elabftw_url,
            self.config.vm_agent_url,
            self.config.poll_interval,
        )

        while self._running:
            try:
                self._poll_cycle()
            except Exception:
                logger.exception("Poll cycle failed")
            time.sleep(self.config.poll_interval)

        self.dashboard.stop()
        logger.info("Bridge daemon stopped")

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._running = False

    def _poll_cycle(self) -> None:
        """One iteration: claim and execute pending jobs."""
        results = self.intake.process_pending()

        for result in results:
            item_id = result["item_id"]

            if result["status"] != "claimed":
                logger.warning(
                    "Job %d not claimed: %s",
                    item_id,
                    result.get("error", {}),
                )
                continue

            job = result.get("job")
            if job is None:
                logger.error("Job %d claimed but no job object returned", item_id)
                continue

            execution_mode = get_field_value(job.extra_fields, "Execution mode")
            protocol_name = job.request_fields.protocol_name
            spec_dict = job.signed_snapshot

            logger.info(
                "Job %d: mode=%s protocol=%s signer=%s",
                item_id,
                execution_mode,
                protocol_name or "(generated)",
                result.get("signer", "?"),
            )

            try:
                exec_result = self.orchestrator.execute_job(
                    item_id,
                    execution_mode,
                    protocol_name,
                    spec_dict,
                )
                logger.info(
                    "Job %d executed: success=%s state=%s",
                    item_id,
                    exec_result.success,
                    exec_result.final_state,
                )
            except Exception:
                logger.exception("Job %d execution failed", item_id)

    def _abort_loop(self) -> None:
        """Background thread: poll eLabFTW for abort requests on running jobs."""
        while self._running:
            try:
                self._check_aborts()
            except Exception:
                logger.exception("Abort poll failed")
            time.sleep(ABORT_POLL_INTERVAL)

    def _check_aborts(self) -> None:
        """Check all running jobs for eLabFTW abort requests."""
        # Get all automation jobs
        jobs = self.elabftw.list_automation_jobs()

        for job in jobs:
            if job.state != JobState.RUNNING.value:
                continue
            if job.request_fields.requested_action != "abort":
                continue

            # Abort detected — find the run_id and call vm-agent
            run_id = get_field_value(job.extra_fields, "Wallac run ID")
            if not run_id:
                logger.warning(
                    "Job %d: abort requested but no run_id found",
                    job.item_id,
                )
                continue

            logger.info("Job %d: abort requested, aborting run %s", job.item_id, run_id)
            try:
                self.vm_agent.abort_run(run_id)
                self.elabftw.patch_metadata(
                    job.item_id,
                    {"Automation state": {"value": JobState.ABORTING.value}},
                )
            except VmAgentError as e:
                if e.status_code == 425:
                    # Too early to abort — the Victor2 requires ~60s before
                    # it honors abort. Retry on the next poll cycle.
                    logger.info(
                        "Job %d: abort too early (run age <60s), will retry next cycle",
                        job.item_id,
                    )
                else:
                    logger.error("Job %d: abort failed: %s", job.item_id, e)
                    self.elabftw.patch_metadata(
                        job.item_id,
                        {
                            "Automation state": {
                                "value": JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value
                            },
                            "Operator hint": {
                                "value": f"Abort failed: {e}. Use the OEM GUI to stop the run."
                            },
                        },
                    )

    def _drain_spool(self) -> None:
        """Retry spooled write-backs on startup."""
        try:
            pending = self.orchestrator.spool.list_pending()
            if not pending:
                return

            logger.info("Found %d pending spool entries", len(pending))
            for entry in pending:
                logger.info(
                    "Spool: job=%d run=%s status=%s retries=%d",
                    entry.job_id,
                    entry.run_id,
                    entry.status,
                    entry.retry_count,
                )

                # Try to re-upload artifacts
                try:
                    self.orchestrator.spool.mark_writing(entry.job_id)
                    for artifact in entry.artifacts:
                        filename = artifact.get("filename", "")
                        if not filename:
                            continue
                        content = self.orchestrator.spool.read_artifact(entry.job_id, filename)
                        if content is None:
                            logger.warning(
                                "Spool: artifact %s not found for job %d",
                                filename,
                                entry.job_id,
                            )
                            continue
                        self.elabftw.upload_file(
                            entry.job_id,
                            filename,
                            content,
                            comment=artifact.get("comment", ""),
                        )
                    self.orchestrator.spool.mark_completed(entry.job_id)
                    logger.info("Spool: job %d write-back completed", entry.job_id)
                except Exception as e:
                    logger.error("Spool: job %d write-back failed: %s", entry.job_id, e)
                    self.orchestrator.spool.increment_retry(entry.job_id)
                    self.orchestrator.spool.mark_failed(entry.job_id, str(e))
        except Exception:
            logger.exception("Spool drain failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config = BridgeConfig.from_env()
    except Exception as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    logger.info("Config: %s", config.redacted())

    daemon = BridgeDaemon(config)

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        daemon.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    daemon.run()


if __name__ == "__main__":
    main()
