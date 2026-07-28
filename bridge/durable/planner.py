"""Writeback planner.

Decomposes the existing `_writeback` method (slice 4 of
``docs/plans/wallac-existing-protocol-writeback-repair.md``) into four
idempotent stages:

    create_experiment
    upload_raw
    upload_analyzed
    patch_body

Each stage has a deterministic idempotency key derived from
``(job_id, action, payload_hash)``. The executor's worker thread
advances the stage machine; on ambiguous HTTP responses it re-checks
the remote state and resumes. The retry worker (see :mod:`.worker`)
runs in the background and re-enqueues transient failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .idempotency import (
    deterministic_attachment_name,
    json_dumps_compact,
    metadata_tag,
    sha256_hex,
    step_idempotency,
)

WRITEBACK_ACTIONS: tuple[str, ...] = (
    "create_experiment",
    "upload_raw",
    "upload_analyzed",
    "patch_body",
)


@dataclass(frozen=True)
class StagePlan:
    """One stage of the writeback outbox.

    ``payload_hash`` is the SHA-256 of the stage's payload (e.g. file
    bytes for an upload, the patched body for the PATCH). The
    idempotency token combines job_id, action, and payload_hash so
    that a retry of an identical stage is a no-op against the remote.
    """

    step_id: str
    job_id: str
    action: str
    idempotency: str
    payload_hash: str


def plan_writeback(
    *,
    job_id: str,
    elabftw_experiment_id: int,
    raw_bytes: bytes | None,
    analyzed_bytes: bytes | None,
    body_html: str,
    metadata_keys: dict[str, str],
) -> list[StagePlan]:
    """Return the four writeback stages in canonical order.

    Skips ``upload_raw`` when ``raw_bytes`` is ``None`` and skips
    ``upload_analyzed`` when ``analyzed_bytes`` is ``None``. Each
    metadata key becomes a stage-level idempotency tag written to
    eLabFTW.
    """
    plan: list[StagePlan] = []

    # create_experiment
    create_payload = json.dumps(
        {"experiment_id": elabftw_experiment_id, "metadata": metadata_keys},
        sort_keys=True,
    )
    plan.append(
        StagePlan(
            step_id=_step_id(job_id, "create_experiment", create_payload),
            job_id=job_id,
            action="create_experiment",
            idempotency=step_idempotency(
                job_id,
                "create_experiment",
                sha256_hex(create_payload.encode()),
            ),
            payload_hash=sha256_hex(create_payload.encode()),
        )
    )

    if raw_bytes is not None:
        plan.append(
            StagePlan(
                step_id=_step_id(job_id, "upload_raw", raw_bytes),
                job_id=job_id,
                action="upload_raw",
                idempotency=step_idempotency(job_id, "upload_raw", sha256_hex(raw_bytes)),
                payload_hash=sha256_hex(raw_bytes),
            )
        )

    if analyzed_bytes is not None:
        plan.append(
            StagePlan(
                step_id=_step_id(job_id, "upload_analyzed", analyzed_bytes),
                job_id=job_id,
                action="upload_analyzed",
                idempotency=step_idempotency(job_id, "upload_analyzed", sha256_hex(analyzed_bytes)),
                payload_hash=sha256_hex(analyzed_bytes),
            )
        )

    # patch_body — payload is the final HTML
    plan.append(
        StagePlan(
            step_id=_step_id(job_id, "patch_body", body_html),
            job_id=job_id,
            action="patch_body",
            idempotency=step_idempotency(job_id, "patch_body", sha256_hex(body_html.encode())),
            payload_hash=sha256_hex(body_html.encode()),
        )
    )

    return plan


def _step_id(job_id: str, action: str, payload: bytes | str) -> str:
    """Composite stage ID: job_id:action:hash_prefix."""
    if isinstance(payload, str):
        payload = payload.encode()
    return f"{job_id}:{action}:{sha256_hex(payload)[:16]}"


# ---------------------------------------------------------------------------
# Operator recovery surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryBundle:
    """Operator-facing payload for a paused job.

    Contains everything an operator needs to manually resolve the
    job without re-acquiring the plate. Never includes API keys,
    private keys, or CA bundles.
    """

    job_id: str
    status: str
    error: str
    elabftw_experiment_id: int
    artifact_count: int
    events: list[dict[str, Any]]
    writeback_step_status: dict[str, str]


def build_recovery_bundle(manager, job_id: str) -> RecoveryBundle:
    """Return the operator-facing recovery payload for a job.

    The manager is expected to expose ``get_job``, ``events``, and
    ``list_steps``; see :class:`JobManager` for the durable side and
    the existing ``JobManager`` for the in-memory side during the
    transition.
    """
    job = manager.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job_id: {job_id!r}")
    step_status: dict[str, str] = {}
    for row in manager.conn.execute(
        "SELECT step_id, status FROM writeback_steps WHERE job_id = ?",
        (job_id,),
    ):
        step_status[row["step_id"]] = row["status"]
    return RecoveryBundle(
        job_id=job.job_id,
        status=job.status,
        error=job.error or "",
        elabftw_experiment_id=job.elabftw_experiment_id,
        artifact_count=len(job.artifacts),
        events=list(job.events),
        writeback_step_status=step_status,
    )


def merge_results_section(existing_body: str, section_html: str, *, job_id: str) -> str:
    """Replace the per-job sentinel section in an existing body.

    Mirrors ``_merge_results_section`` in the in-memory ``_writeback``:
    the new section is bracketed by
    ``<!-- WALLAC_RESULTS:<job_id>:START -->`` /
    ``<!-- WALLAC_RESULTS:<job_id>:END -->`` and replaces whatever was
    inside those markers. Anything outside the markers is preserved
    verbatim.
    """
    start = f"<!-- WALLAC_RESULTS:{job_id}:START -->"
    end = f"<!-- WALLAC_RESULTS:{job_id}:END -->"
    block = f"{start}\n{section_html}\n{end}"
    i = existing_body.find(start)
    j = existing_body.find(end)
    if i == -1 or j == -1 or j < i:
        # No prior section; append. Caller has already composed the
        # body so a leading newline keeps the layout tidy.
        sep = "" if existing_body.endswith("\n") else "\n"
        return f"{existing_body}{sep}{block}\n"
    head = existing_body[:i]
    tail = existing_body[j + len(end) :]
    sep = "" if tail.startswith("\n") else "\n"
    return f"{head}{block}{sep}{tail}".rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Re-export useful idempotency helpers for the executor.
# ---------------------------------------------------------------------------


__all__ = [
    "WRITEBACK_ACTIONS",
    "RecoveryBundle",
    "StagePlan",
    "build_recovery_bundle",
    "deterministic_attachment_name",
    "json_dumps_compact",
    "merge_results_section",
    "metadata_tag",
    "plan_writeback",
]
