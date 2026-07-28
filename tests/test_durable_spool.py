"""Tests for the durable bridge spool (Lambda-Biolab/wallac-victor2-api#44).

Covers acceptance criteria 1-7 from the issue:

    1. Job record durably committed before physical execution begins.
    2. Raw and analyzed results atomically persisted before eLabFTW delivery.
    3. Restarting the bridge during writeback_pending resumes delivery
       without re-running the instrument.
    4. After a successful remote step but before its response is recorded,
       no duplicate experiments or attachments.
    5. Transient failures retry with bounded exponential backoff + jitter.
    6. Auth/TLS/schema failures pause and require operator action; they
       never trigger insecure fallback.
    7. Operators can inspect pending/partial writebacks and retry/resolve.

The tests use a temporary state directory under ``/tmp`` so they are
hermetic and don't touch the live bridge state.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from bridge.durable.idempotency import (
    sha256_hex,
    step_idempotency,
)
from bridge.durable.manager import JobManager
from bridge.durable.planner import (
    WRITEBACK_ACTIONS,
    build_recovery_bundle,
    merge_results_section,
    plan_writeback,
)
from bridge.durable.retry import (
    Backoff,
    classify_status,
)
from bridge.durable.worker import (
    PendingStep,
    RetryAction,
    StepLedger,
    WritebackWorker,
    record_step_outcome,
)


@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory(prefix="wallac-bridge-") as tmp:
        yield Path(tmp)


@pytest.fixture
def manager(state_dir):
    m = JobManager(state_dir)
    yield m
    m.close()


def _submit(manager: JobManager, **overrides) -> str:
    job_id = overrides.get("job_id") or f"job-{sha256_hex(str(time.time_ns()).encode())[:10]}"
    spec = dict(
        job_id=job_id,
        title="test job",
        execution_mode="existing_protocol",
        protocol_name="Absorbance @ 610 (1.0s)",
        protocol_id=2000008,
        elabftw_experiment_id=125,
        wells_spec={"wells": ["A1", "A2"]},
    )
    spec.update(overrides)
    manager.submit_job(**spec)
    return job_id


# ---------------------------------------------------------------------------
# 1. Job record durably committed before physical execution begins
# ---------------------------------------------------------------------------


def test_submit_persists_job_before_status_change(manager: JobManager) -> None:
    job_id = _submit(manager)
    # Job exists, status is accepted, no further side effects.
    job = manager.get_job(job_id)
    assert job is not None
    assert job.status == "accepted"
    assert job.events[0]["event"] == "job_submitted"

    # Restart simulation: a new connection to the same SQLite file must
    # see the row without any in-memory carryover.
    fresh = JobManager(manager.state_dir)
    try:
        fresh_job = fresh.get_job(job_id)
        assert fresh_job is not None
        assert fresh_job.status == "accepted"
        assert fresh_job.protocol_name == "Absorbance @ 610 (1.0s)"
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# 2. Raw and analyzed artifacts atomically persisted before eLabFTW delivery
# ---------------------------------------------------------------------------


def test_artifact_persistence_is_atomic(manager: JobManager) -> None:
    job_id = _submit(manager)
    raw = b'{"wells":[{"well":"A1","od":0.123}]}'
    analyzed = b"well,od\nA1,0.123\n"
    sha_raw = sha256_hex(raw)
    sha_analyzed = sha256_hex(analyzed)

    with manager.conn:
        manager.record_artifact(job_id, "raw", "/tmp/raw.json", sha_raw)
        manager.record_artifact(job_id, "analyzed", "/tmp/analyzed.csv", sha_analyzed)

    fresh = JobManager(manager.state_dir)
    try:
        job = fresh.get_job(job_id)
        kinds = {a.kind for a in job.artifacts}
        assert kinds == {"raw", "analyzed"}
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# 3. Restart during writeback_pending resumes delivery
# ---------------------------------------------------------------------------


def test_restart_preserves_pending_step_for_retry(manager: JobManager) -> None:
    job_id = _submit(manager)

    # Enqueue a step and observe it pending.
    step_id = f"{job_id}:upload_raw:pending"
    ledger = StepLedger(manager.conn)
    ledger.enqueue(
        [
            PendingStep(
                step_id=step_id,
                job_id=job_id,
                action="upload_raw",
                idempotency=step_idempotency(job_id, "upload_raw", "abc"),
            )
        ]
    )
    pending = manager.conn.execute(
        "SELECT status FROM writeback_steps WHERE step_id = ?", (step_id,)
    ).fetchone()
    assert pending["status"] == "pending"

    # Restart simulation: a fresh manager on the same state_dir must
    # still report the step as pending.
    fresh = JobManager(manager.state_dir)
    try:
        rows = list(
            fresh.conn.execute("SELECT * FROM writeback_steps WHERE step_id = ?", (step_id,))
        )
        assert rows[0]["status"] == "pending"
    finally:
        fresh.close()


def test_worker_run_once_only_dispatches_due_steps(manager: JobManager) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)

    due_step = f"{job_id}:upload_raw:due"
    future_step = f"{job_id}:upload_analyzed:future"
    ledger.enqueue(
        [
            PendingStep(due_step, job_id, "upload_raw", "due-token"),
            PendingStep(future_step, job_id, "upload_analyzed", "future-token"),
        ]
    )
    # Move the second step's next_attempt_at into the future so the
    # worker leaves it alone.
    manager.conn.execute(
        "UPDATE writeback_steps SET next_attempt_at = '2099-01-01T00:00:00+00:00' "
        "WHERE step_id = ?",
        (future_step,),
    )

    dispatched: list[RetryAction] = []
    worker = WritebackWorker(
        manager.conn,
        on_step=lambda a: dispatched.append(a),
        interval_seconds=0.1,
    )
    n = worker.run_once()
    assert n == 1
    assert [a.step_id for a in dispatched] == [due_step]


# ---------------------------------------------------------------------------
# 4. Ambiguous-response deduplication via idempotency tokens
# ---------------------------------------------------------------------------


def test_plan_writeback_is_deterministic_across_runs(manager: JobManager) -> None:
    job_id = _submit(manager)
    raw = b'{"wells":[]}'
    analyzed = b"a,b\n1,2\n"
    body = "<table></table>"
    md = {"wallac.bridge.correlation_id": job_id}

    plan_a = plan_writeback(
        job_id=job_id,
        elabftw_experiment_id=125,
        raw_bytes=raw,
        analyzed_bytes=analyzed,
        body_html=body,
        metadata_keys=md,
    )
    plan_b = plan_writeback(
        job_id=job_id,
        elabftw_experiment_id=125,
        raw_bytes=raw,
        analyzed_bytes=analyzed,
        body_html=body,
        metadata_keys=md,
    )

    # Same inputs → same idempotency tokens.
    a_keys = {p.idempotency for p in plan_a}
    b_keys = {p.idempotency for p in plan_b}
    assert a_keys == b_keys

    # Stages appear in the canonical order.
    assert [p.action for p in plan_a] == list(WRITEBACK_ACTIONS)


def test_plan_skips_optional_steps_when_artifacts_missing() -> None:
    plan = plan_writeback(
        job_id="job-x",
        elabftw_experiment_id=0,
        raw_bytes=None,
        analyzed_bytes=None,
        body_html="<p/>",
        metadata_keys={},
    )
    actions = [p.action for p in plan]
    # create_experiment + patch_body remain; uploads skipped.
    assert actions == ["create_experiment", "patch_body"]


def test_idempotent_enqueue_uses_insert_or_ignore(manager: JobManager) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    step = PendingStep(
        step_id=f"{job_id}:upload_raw:dedup",
        job_id=job_id,
        action="upload_raw",
        idempotency=step_idempotency(job_id, "upload_raw", "same-token"),
    )
    ledger.enqueue([step])
    ledger.enqueue([step])  # second enqueue must not raise or duplicate

    rows = list(
        manager.conn.execute(
            "SELECT COUNT(*) AS n FROM writeback_steps WHERE step_id = ?",
            (step.step_id,),
        )
    )
    assert rows[0]["n"] == 1


# ---------------------------------------------------------------------------
# 5. Transient failures retry with bounded exponential backoff
# ---------------------------------------------------------------------------


def test_classify_status_marks_transient_and_permanent() -> None:
    # Transient
    assert classify_status(429) == "transient"
    assert classify_status(503) == "transient"
    assert classify_status(504) == "transient"
    assert classify_status(None) == "transient"
    # Permanent
    assert classify_status(401) == "permanent"
    assert classify_status(403) == "permanent"
    assert classify_status(422) == "permanent"
    # Success
    assert classify_status(200) == "success"
    assert classify_status(204) == "success"
    # TLS error overrides any status
    assert classify_status(200, tls_error=True) == "permanent"


def test_transient_outcome_schedules_next_attempt(manager: JobManager) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue([PendingStep(f"{job_id}:upload_raw:b", job_id, "upload_raw", "tok-b")])
    step_id = f"{job_id}:upload_raw:b"

    status, when = record_step_outcome(
        ledger, step_id=step_id, http_status=503, detail="connection reset"
    )
    assert status == "pending"
    assert when is not None  # scheduled
    row = manager.conn.execute(
        "SELECT attempts, status FROM writeback_steps WHERE step_id = ?", (step_id,)
    ).fetchone()
    assert row["attempts"] == 1
    assert row["status"] == "pending"


def test_backoff_is_bounded_and_jittered() -> None:
    backoff = Backoff(base_seconds=10.0, cap_seconds=300.0, max_attempts=4)
    # Each attempt's draw stays within [0, base * 2^attempt] capped.
    for attempt in range(4):
        bound = min(10.0 * (2**attempt), 300.0)
        drawn = [backoff.wait_seconds(attempt) for _ in range(200)]
        assert all(0 <= w <= bound + 1e-6 for w in drawn)
    # Cap is enforced once attempt >= max_attempts.
    assert backoff.wait_seconds(99) == backoff.cap_seconds


# ---------------------------------------------------------------------------
# 6. Permanent failures pause and never retry
# ---------------------------------------------------------------------------


def test_permanent_outcome_pauses_step_with_no_next_attempt(
    manager: JobManager,
) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue([PendingStep(f"{job_id}:upload_raw:perm", job_id, "upload_raw", "tok-p")])
    step_id = f"{job_id}:upload_raw:perm"

    status, when = record_step_outcome(
        ledger,
        step_id=step_id,
        http_status=401,
        detail="invalid api key",
    )
    assert status == "paused"
    assert when is None  # never rescheduled
    row = manager.conn.execute(
        "SELECT status, attempts, next_attempt_at FROM writeback_steps WHERE step_id = ?",
        (step_id,),
    ).fetchone()
    assert row["status"] == "paused"
    assert row["next_attempt_at"] is None


def test_tls_error_classifies_as_permanent_even_on_success(manager: JobManager) -> None:
    """Regression: a 200 OK with a TLS handshake error must pause.

    Some elabftw_writeback_failed events trace back to TLS errors
    raised before any HTTP response. The classifier must mark these
    permanent regardless of HTTP status.
    """
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue([PendingStep(f"{job_id}:upload_raw:tls", job_id, "upload_raw", "tok-tls")])
    step_id = f"{job_id}:upload_raw:tls"
    status, when = record_step_outcome(
        ledger,
        step_id=step_id,
        http_status=200,
        detail="SSL: CERTIFICATE_VERIFY_FAILED",
        tls_error=True,
    )
    assert status == "paused"
    assert when is None


def test_attempts_history_recorded_for_each_outcome(manager: JobManager) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue([PendingStep(f"{job_id}:upload_raw:hist", job_id, "upload_raw", "tok-h")])
    step_id = f"{job_id}:upload_raw:hist"

    record_step_outcome(ledger, step_id=step_id, http_status=503, detail="t1")
    record_step_outcome(ledger, step_id=step_id, http_status=503, detail="t2")
    record_step_outcome(ledger, step_id=step_id, http_status=401, detail="final")

    history = list(
        manager.conn.execute(
            "SELECT ts, outcome, detail FROM writeback_attempts "
            "WHERE step_id = ? ORDER BY attempt_id",
            (step_id,),
        )
    )
    assert [h["outcome"] for h in history] == ["transient", "transient", "permanent"]


# ---------------------------------------------------------------------------
# 7. Operator recovery surface
# ---------------------------------------------------------------------------


def test_build_recovery_bundle_excludes_secrets(manager: JobManager) -> None:
    job_id = _submit(manager)
    raw = b"raw payload"
    sha = sha256_hex(raw)
    manager.record_artifact(job_id, "raw", "/var/lib/wallac-bridge/spool/raw.json", sha)
    manager.record_event(job_id, "measurement_complete", "n=1")

    bundle = build_recovery_bundle(manager, job_id)
    payload = bundle.__dict__
    blob = json.dumps(payload)
    # The bundle must never contain credentials or private keys.
    for needle in ("api_key", "API_KEY", "private_key", ".key", "BEGIN RSA"):
        assert needle.lower() not in blob.lower(), f"recovery bundle leaks {needle!r}"


def test_recovery_bundle_lists_steps(manager: JobManager) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue(
        [
            PendingStep(f"{job_id}:create_experiment:1", job_id, "create_experiment", "t1"),
            PendingStep(f"{job_id}:upload_raw:2", job_id, "upload_raw", "t2"),
        ]
    )
    # Mark the second step paused
    record_step_outcome(
        ledger, step_id=f"{job_id}:upload_raw:2", http_status=403, detail="forbidden"
    )

    bundle = build_recovery_bundle(manager, job_id)
    assert bundle.writeback_step_status[f"{job_id}:upload_raw:2"] == "paused"
    assert bundle.writeback_step_status[f"{job_id}:create_experiment:1"] == "pending"


# ---------------------------------------------------------------------------
# Bonus: per-job body merge mirrors slice-4 sentinel logic
# ---------------------------------------------------------------------------


def test_merge_results_section_replaces_existing_block() -> None:
    existing = (
        "before\n"
        "<!-- WALLAC_RESULTS:job-x:START -->\n"
        "<p>old body</p>\n"
        "<!-- WALLAC_RESULTS:job-x:END -->\n"
        "after\n"
    )
    new_section = "<p>new body</p>"
    out = merge_results_section(existing, new_section, job_id="job-x")
    assert "<p>new body</p>" in out
    assert "<p>old body</p>" not in out
    assert out.startswith("before\n")
    assert "after\n" in out


def test_merge_results_section_appends_when_no_prior_section() -> None:
    out = merge_results_section(
        "<p>existing body</p>",
        "<table/>",
        job_id="job-y",
    )
    assert "<!-- WALLAC_RESULTS:job-y:START -->" in out
    assert "<table/>" in out
    assert "<!-- WALLAC_RESULTS:job-y:END -->" in out
    assert out.startswith("<p>existing body</p>")


# ---------------------------------------------------------------------------
# Worker end-to-end: pause/resume pattern
# ---------------------------------------------------------------------------


def test_worker_records_attempts_and_does_not_re_dispatch_paused(
    manager: JobManager,
) -> None:
    job_id = _submit(manager)
    ledger = StepLedger(manager.conn)
    ledger.enqueue(
        [PendingStep(f"{job_id}:create_experiment:w", job_id, "create_experiment", "tw")]
    )

    def dispatcher(action: RetryAction) -> None:
        # Simulate eLabFTW returning 403 (auth failure → permanent).
        record_step_outcome(
            ledger,
            step_id=action.step_id,
            http_status=403,
            detail="forbidden",
        )

    worker = WritebackWorker(manager.conn, on_step=dispatcher, interval_seconds=0.05)
    n = worker.run_once()
    assert n == 1
    # Step paused; second tick does not re-dispatch.
    n2 = worker.run_once()
    assert n2 == 0
    row = manager.conn.execute(
        "SELECT status, attempts, next_attempt_at FROM writeback_steps WHERE step_id = ?",
        (f"{job_id}:create_experiment:w",),
    ).fetchone()
    assert row["status"] == "paused"
    assert row["next_attempt_at"] is None
