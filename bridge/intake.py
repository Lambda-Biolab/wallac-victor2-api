"""Automation Job intake: polling, signature verification, and claiming.

Implements the bridge-side intake path for signed eLabFTW Automation Jobs
(GitHub issue #2).  The intake:

  1. Polls eLabFTW for Automation Jobs where service=wallac_victor2,
     action=submit, state=requested.
  2. Verifies the eLabFTW signature before claim: signer identity, timestamp,
     and whether signed request fields changed afterward.
  3. Fails closed with operator-review status if signature metadata cannot
     be verified programmatically.
  4. Snapshots the signed request fields before execution.
  5. Claims jobs atomically enough to avoid double execution by multiple
     bridge instances.
  6. Populates Wallac run identifiers and Live Monitor URL after claim.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .elabftw import ElabftwInterface
from .errors import (
    ALREADY_CLAIMED,
    REQUEST_MODIFIED_AFTER_SIGNATURE,
    SIGNATURE_VERIFICATION_FAILED,
    UNSIGNED_JOB,
    BridgeError,
    Severity,
)
from .models import AutomationJob, JobState
from .signature import (
    extract_signature_info,
    extract_signed_request_fields,
    find_signature_upload,
    parse_signature_archive,
    verify_signature,
)

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobIntake:
    """Polls eLabFTW for claimable Automation Jobs and claims them.

    The intake is the entry point for the bridge's async job lifecycle.
    After :meth:`process_pending` claims a job, downstream components
    (preflight, execution, write-back) take over.
    """

    def __init__(
        self,
        client: ElabftwInterface,
        bridge_identity: str,
        live_monitor_url_base: str,
    ) -> None:
        self.client = client
        self.bridge_identity = bridge_identity
        self.live_monitor_url_base = live_monitor_url_base.rstrip("/")

    # --- Polling ------------------------------------------------------------

    def find_claimable_jobs(self) -> list[AutomationJob]:
        """Return Automation Jobs that match the claim criteria.

        A job is claimable when:
          - Automation service = wallac_victor2
          - Requested action = submit
          - Automation state = requested
        """
        all_jobs = self.client.list_automation_jobs()
        return [
            job
            for job in all_jobs
            if job.request_fields.automation_service == "wallac_victor2"
            and job.request_fields.requested_action == "submit"
            and job.state == JobState.REQUESTED.value
        ]

    # --- Signature verification ---------------------------------------------

    def verify_job_signature(self, job: AutomationJob) -> AutomationJob:
        """Verify the eLabFTW signature on a job.

        Raises :class:`BridgeError` if:
          - The job has no signature archive (``UNSIGNED_JOB``).
          - The cryptographic signature is invalid (``SIGNATURE_VERIFICATION_FAILED``).
          - Request fields were modified after signing
            (``REQUEST_MODIFIED_AFTER_SIGNATURE``).

        On success, populates ``job.signature_info`` and
        ``job.signed_snapshot``.
        """
        # 1. Find the signature archive upload
        uploads = self.client.list_uploads(job.item_id)
        sig_upload = find_signature_upload(uploads)
        if sig_upload is None:
            raise BridgeError(
                code=UNSIGNED_JOB,
                severity=Severity.ERROR,
                human_message=(f"Automation Job {job.item_id} has no signature archive"),
                operator_hint=(
                    "An authorized operator must sign the Automation Job "
                    "before the bridge can claim it."
                ),
                details={"item_id": job.item_id},
            )

        # 2. Download and parse the signature archive
        zip_bytes = self.client.download_upload(job.item_id, sig_upload["id"])
        data_json, minisig, pubkey = parse_signature_archive(zip_bytes)

        # 3. Verify the cryptographic signature
        if not verify_signature(data_json, minisig, pubkey):
            raise BridgeError(
                code=SIGNATURE_VERIFICATION_FAILED,
                severity=Severity.ERROR,
                human_message=(f"Signature verification failed for Automation Job {job.item_id}"),
                operator_hint=(
                    "The signature archive may be corrupted or the signing "
                    "key may have been revoked."
                ),
                details={"item_id": job.item_id, "upload_id": sig_upload["id"]},
            )

        # 4. Extract signer metadata
        job.signature_info = extract_signature_info(minisig)
        job.signed_snapshot = json.loads(data_json)

        # 5. Check for post-signature modifications
        signed_fields = extract_signed_request_fields(data_json)
        if signed_fields.to_dict() != job.request_fields.to_dict():
            raise BridgeError(
                code=REQUEST_MODIFIED_AFTER_SIGNATURE,
                severity=Severity.ERROR,
                human_message=(
                    f"Request fields were modified after signature on Automation Job {job.item_id}"
                ),
                operator_hint=(
                    "The signed request fields are immutable. Re-sign the job after making changes."
                ),
                details={
                    "item_id": job.item_id,
                    "signed_fields": signed_fields.to_dict(),
                    "current_fields": job.request_fields.to_dict(),
                },
            )

        logger.info(
            "Job %d signature verified: signer=%s, meaning=%s, signed_at=%s",
            job.item_id,
            job.signature_info.signer_email,
            job.signature_info.meaning,
            job.signature_info.signed_at,
        )
        return job

    # --- Claiming ------------------------------------------------------------

    def claim_job(self, job: AutomationJob) -> str:
        """Claim a verified job by writing back state and identity fields.

        Sets ``Automation state = accepted``, ``Claimed by``,
        ``Claimed at``, and ``Live Monitor`` URL.

        Returns the Live Monitor URL.

        Raises :class:`BridgeError` with ``ALREADY_CLAIMED`` if the job's
        state is no longer ``requested`` (another instance claimed it first).
        """
        live_monitor_url = f"{self.live_monitor_url_base}/jobs/{job.item_id}"

        # Re-read current state to detect a race (duplicate-claim prevention).
        # This is "atomic enough" per the AC: the eLabFTW API does not expose
        # conditional updates, so we re-check state before patching.
        current_jobs = self.client.list_automation_jobs()
        current = next((j for j in current_jobs if j.item_id == job.item_id), None)
        if current is None:
            raise BridgeError(
                code=ALREADY_CLAIMED,
                human_message=f"Automation Job {job.item_id} no longer exists",
                details={"item_id": job.item_id},
            )
        if current.state != JobState.REQUESTED.value:
            raise BridgeError(
                code=ALREADY_CLAIMED,
                human_message=(
                    f"Automation Job {job.item_id} is no longer in 'requested' "
                    f"state (current: {current.state})"
                ),
                operator_hint="Another bridge instance may have already claimed this job.",
                details={"item_id": job.item_id, "current_state": current.state},
            )

        self.client.patch_metadata(
            job.item_id,
            {
                "Automation state": {
                    "type": "select",
                    "value": JobState.ACCEPTED.value,
                },
                "Claimed by": {
                    "type": "text",
                    "value": self.bridge_identity,
                },
                "Claimed at": {
                    "type": "datetime-local",
                    "value": now_iso(),
                },
                "Live Monitor": {
                    "type": "url",
                    "value": live_monitor_url,
                },
            },
        )

        logger.info("Job %d claimed by %s", job.item_id, self.bridge_identity)
        return live_monitor_url

    # --- Rejection write-back ------------------------------------------------

    def _write_rejection(self, job: AutomationJob, error: BridgeError) -> None:
        """Write back a rejection/error state to eLabFTW."""
        try:
            self.client.patch_metadata(
                job.item_id,
                {
                    "Automation state": {
                        "type": "select",
                        "value": JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value,
                    },
                    "Last error code": {
                        "type": "text",
                        "value": error.code,
                    },
                    "Operator hint": {
                        "type": "text",
                        "value": error.operator_hint,
                    },
                },
            )
        except Exception:
            logger.exception("Failed to write rejection for job %d", job.item_id)

    # --- Top-level intake ----------------------------------------------------

    def process_pending(self) -> list[dict[str, Any]]:
        """Poll, verify, and claim all pending Automation Jobs.

        Returns a list of result dicts with ``item_id``, ``status``, and
        any error details.  Jobs that fail verification are written back
        with ``unknown_requires_operator_review`` state.
        """
        results: list[dict[str, Any]] = []
        for job in self.find_claimable_jobs():
            result: dict[str, Any] = {"item_id": job.item_id, "title": job.title}
            try:
                self.verify_job_signature(job)
                live_url = self.claim_job(job)
                result["status"] = "claimed"
                result["live_monitor_url"] = live_url
                result["job"] = job  # full AutomationJob with extra_fields + signed_snapshot
                if job.signature_info:
                    result["signer"] = job.signature_info.signer_email
            except BridgeError as e:
                result["status"] = "rejected"
                result["error"] = e.to_dict()
                self._write_rejection(job, e)
            results.append(result)
        return results
