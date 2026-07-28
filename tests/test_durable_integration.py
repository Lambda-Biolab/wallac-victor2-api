"""End-to-end integration test for the durable writeback spool.

This test exercises the full issue #44 acceptance flow:

1. The executor runs a Wallac job to ``measured`` (simulated by
   calling ``_durable_writeback`` directly with canned wells).
2. The durable branch spools raw/analyzed/body artifacts to the
   state directory, records them in the durable ledger, and
   enqueues the four canonical writeback steps.
3. A ``WritebackDispatcher`` + ``WritebackWorker`` constructed
   against the same ledger dispatch all four steps; the
   ``MockElabftwClient`` records the eLabFTW operations.
4. Idempotency: re-dispatching the same steps is a no-op against
   the remote (artifacts flagged ``uploaded=1``), and a "restart"
   represented by a fresh dispatcher on the same ``state_dir``
   resumes from the next pending step.
5. ``max_attempts=8`` is enforced: a flaky upload that always
   raises a transient HTTP error eventually pauses the step
   instead of looping forever.

This is the load-bearing test for the
``@review-pr`` ``REQUEST CHANGES`` against PR #53 (issue #44).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bridge.durable.dispatcher import WritebackDispatcher
from bridge.durable.manager import JobManager, now_iso
from bridge.durable.worker import StepLedger, WritebackWorker
from bridge.executor import BridgeExecutor
from bridge.jobs import Job
from tests.test_executor import MockElabftwClient, MockVmAgentClient

# A canned run that returns two wells. The executor's
# ``_durable_writeback`` will write these to disk as raw.json,
# analyse them, and spool the body.html.
CANNED_WELLS = [
    {"well_name": "A1", "value": 0.078, "counts": 684016},
    {"well_name": "A2", "value": 0.089, "counts": 666875},
]


@pytest.fixture
def state_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="wallac-bridge-e2e-") as tmp:
        yield Path(tmp)


@pytest.fixture
def durable_manager(state_dir: Path) -> Iterator[JobManager]:
    m = JobManager(state_dir)
    try:
        yield m
    finally:
        m.close()


def _make_job(job_id: str = "job-e2e-1") -> Job:
    """Build an in-memory ``Job`` ready for the executor's writeback.

    ``_durable_writeback`` only reads a small surface: title, job_id,
    elabftw_experiment_id, and it appends to ``artifacts``. We seed
    those fields directly.
    """
    return Job(
        job_id=job_id,
        title="e2e existing protocol",
        execution_mode="existing_protocol",
        protocol_id=2000008,
        protocol_name="Absorbance @ 610 (1.0s)",
        elabftw_experiment_id=0,  # create_experiment path
    )


def _durable_record_for(durable: JobManager, job: Job) -> None:
    """Create the durable row that mirrors the in-memory job.

    In production the request handler does this; the test calls
    the public API directly to keep the test focused on the
    writeback path.
    """
    durable.submit_job(
        job_id=job.job_id,
        title=job.title,
        execution_mode=job.execution_mode,
        protocol_name=job.protocol_name,
        protocol_id=job.protocol_id,
        elabftw_experiment_id=job.elabftw_experiment_id,
        wells_spec={"wells": ["A1", "A2"]},
    )


def _build_executor(
    elabftw: MockElabftwClient, vm_agent: MockVmAgentClient, *, durable: JobManager
) -> BridgeExecutor:
    return BridgeExecutor(
        vm_agent=vm_agent,
        elabftw=elabftw,
        dry_run=False,
        durable_manager=durable,
        durable_ledger=StepLedger(durable.conn),
    )


# --- Tests ----------------------------------------------------------------


def test_durable_writeback_full_flow(durable_manager: JobManager) -> None:
    """Spool → enqueue → worker dispatches all 4 stages → all done.

    Acceptance (review blocker #1): the four eLabFTW operations all
    happen via the worker; the durable ledger records them as
    ``done``; the artifacts are flagged ``uploaded=1`` so a retry
    is a no-op.
    """
    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-full")
    _durable_record_for(durable_manager, job)

    # The executor's durable writeback spools the artifacts and
    # enqueues the four canonical writeback steps.
    analyzed_csv = "well,value\nA1,0.078\nA2,0.089\n"
    success = executor._durable_writeback(job, CANNED_WELLS, analyzed_csv)
    assert success is True

    # The four steps are pending, in the canonical order.
    steps = list(
        durable_manager.conn.execute(
            "SELECT step_id, action, status FROM writeback_steps WHERE job_id = ? ORDER BY step_id",
            (job.job_id,),
        )
    )
    actions = [s["action"] for s in steps]
    assert actions == ["create_experiment", "patch_body", "upload_analyzed", "upload_raw"]
    assert all(s["status"] == "pending" for s in steps)

    # The artifacts are spooled and recorded in the durable ledger.
    raw_artifact = durable_manager.find_artifact(job.job_id, "raw")
    analyzed_artifact = durable_manager.find_artifact(job.job_id, "analyzed")
    body_artifact = durable_manager.find_artifact(job.job_id, "body")
    assert raw_artifact is not None and Path(raw_artifact.path).exists()
    assert analyzed_artifact is not None and Path(analyzed_artifact.path).exists()
    assert body_artifact is not None and Path(body_artifact.path).exists()

    # The durable job is now ``writeback_pending``.
    assert durable_manager.get_job(job.job_id).status == "writeback_pending"

    # Dispatcher + worker. Single-pass: one ``run_once`` per step.
    completed: list[str] = []

    def _on_all_done(job_id: str) -> None:
        completed.append(job_id)
        durable_manager.mark_status(job_id, "completed", completed_at=now_iso())

    dispatcher = WritebackDispatcher(durable_manager, elabftw, on_all_steps_done=_on_all_done)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    for _ in range(len(steps)):
        worker.run_once()

    # All four eLabFTW operations hit the mock client.
    assert f"{job.job_id}_raw_results.json" in elabftw.uploaded_files
    assert f"{job.job_id}_analyzed.csv" in elabftw.uploaded_files
    # Body PATCH happened — at least one experiment now has the
    # WALLAC_RESULTS marker section.
    assert any("WALLAC_RESULTS" in exp.get("body", "") for exp in elabftw._experiments.values()), (
        "body PATCH should write the WALLAC_RESULTS marker section"
    )

    # All steps are done; the durable job is completed.
    final = list(
        durable_manager.conn.execute(
            "SELECT status FROM writeback_steps WHERE job_id = ?",
            (job.job_id,),
        )
    )
    assert all(s["status"] == "done" for s in final)
    assert completed == [job.job_id]
    assert durable_manager.get_job(job.job_id).status == "completed"


def test_durable_writeback_is_idempotent_on_replay(
    durable_manager: JobManager,
) -> None:
    """Replaying after the artifact is flagged ``uploaded=1`` must NOT
    re-call eLabFTW.

    Acceptance: a forced restart that re-dispatches the same steps
    is a no-op against the remote. The dispatcher checks the
    ``uploaded`` flag and returns immediately.
    """
    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-replay")
    _durable_record_for(durable_manager, job)

    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    for _ in range(10):  # plenty of ticks to drain every step
        worker.run_once()

    uploaded_after_first_run = list(elabftw.uploaded_files)
    assert f"{job.job_id}_raw_results.json" in uploaded_after_first_run
    assert f"{job.job_id}_analyzed.csv" in uploaded_after_first_run

    # Simulate a process restart: fresh dispatcher, same ledger.
    # The upload steps are already ``done`` and the artifacts are
    # flagged ``uploaded=1``; a fresh ``run_once`` finds nothing
    # pending and does nothing.
    dispatcher2 = WritebackDispatcher(durable_manager, elabftw)
    worker2 = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher2.dispatch,
        interval_seconds=0.0,
    )
    for _ in range(5):
        worker2.run_once()

    # No additional uploads happened on the replay.
    assert elabftw.uploaded_files == uploaded_after_first_run


def test_max_attempts_pauses_step_instead_of_looping_forever(
    durable_manager: JobManager,
) -> None:
    """A step that always raises a transient HTTP error must pause
    after ``Backoff.max_attempts`` attempts, not loop indefinitely.

    Acceptance (review blocker #2): the ledger marks the step
    ``paused`` once the attempt budget is exhausted.

    The mock raises ``urllib.error.HTTPError(503)`` on every upload
    call. The dispatcher catches ``HTTPError`` and classifies the
    outcome (transient), so the worker keeps retrying with
    exponential backoff. After ``max_attempts`` attempts, the
    ``record_outcome`` fix in ``worker.py`` pauses the step instead
    of scheduling another pending attempt.
    """
    import urllib.error

    class Transient503Elabftw(MockElabftwClient):
        def upload_experiment_file(  # type: ignore[override]
            self,
            exp_id: int,
            filename: str,
            content: bytes,
            comment: str = "",
        ) -> dict[str, Any]:
            raise urllib.error.HTTPError(
                url=f"http://elabftw/experiments/{exp_id}/uploads",
                code=503,
                msg="Service Unavailable",
                hdrs={},  # type: ignore[arg-type]
                fp=None,
            )

    elabftw = Transient503Elabftw()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-budget")
    _durable_record_for(durable_manager, job)

    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    # Force the bounded backoff to schedule the next attempt in
    # the past so the step is immediately due on every tick. We
    # only care about the attempt-count enforcement, not the
    # backoff timing here.
    import bridge.durable.worker as _worker_mod

    original_wait = _worker_mod.Backoff.wait_seconds

    def _instant_wait(self: _worker_mod.Backoff, attempt: int) -> float:
        return -1.0  # any negative value flips next_attempt_at into the past

    _worker_mod.Backoff.wait_seconds = _instant_wait  # type: ignore[method-assign]
    try:
        for _ in range(20):
            worker.run_once()
    finally:
        _worker_mod.Backoff.wait_seconds = original_wait  # type: ignore[method-assign]

    upload_steps = list(
        durable_manager.conn.execute(
            "SELECT step_id, status, attempts FROM writeback_steps "
            "WHERE job_id = ? AND action IN ('upload_raw', 'upload_analyzed') "
            "ORDER BY step_id",
            (job.job_id,),
        )
    )
    for step in upload_steps:
        assert step["status"] == "paused", (
            f"step {step['step_id']!r} should be paused after exhausted "
            f"attempts, got {step['status']!r}"
        )
        assert step["attempts"] >= 8, (
            f"step {step['step_id']!r} should have >=8 attempts, got {step['attempts']!r}"
        )


def test_classify_status_error_kind_marks_permanent() -> None:
    """``classify_status(error_kind=...)`` returns ``permanent`` even
    when the HTTP status would otherwise map to transient.

    Acceptance (review blocker #3a): schema/CA/auth/TLS errors do
    not retry silently.
    """
    from bridge.durable.retry import classify_status

    # 200 + schema error_kind = permanent (override)
    assert classify_status(200, error_kind="schema") == "permanent"
    # 503 (would be transient) + ca_bundle = permanent
    assert classify_status(503, error_kind="ca_bundle") == "permanent"
    # 409 (would be transient) + auth = permanent
    assert classify_status(409, error_kind="auth") == "permanent"
    # tls_error flag still works
    assert classify_status(200, tls_error=True) == "permanent"
    # No error_kind, plain transient status
    assert classify_status(503) == "transient"
    # No error_kind, plain success
    assert classify_status(200) == "success"
    # Unknown error_kind falls through to HTTP logic
    assert classify_status(503, error_kind="not_a_known_kind") == "transient"


def test_dispatcher_exception_pauses_step(durable_manager: JobManager) -> None:
    """If the dispatcher callback raises, ``run_once`` must mark the
    step ``paused`` so the next tick does not loop on the same bug.

    Acceptance (review blocker #3b): a dispatcher exception is a
    bug, not a transient network failure.
    """
    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-broken")
    _durable_record_for(durable_manager, job)

    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")

    def broken_dispatch(action: Any) -> None:
        raise RuntimeError("simulated dispatcher bug")

    worker = WritebackWorker(
        durable_manager.conn,
        on_step=broken_dispatch,
        interval_seconds=0.0,
    )
    # Several ticks: the first step the worker tried raised, so
    # subsequent ticks find nothing pending.
    for _ in range(3):
        worker.run_once()

    rows = list(
        durable_manager.conn.execute(
            "SELECT step_id, status, attempts FROM writeback_steps WHERE job_id = ?",
            (job.job_id,),
        )
    )
    # Every step the worker picked up must be ``paused`` (permanent
    # outcome), not ``pending`` (which would loop every tick).
    for row in rows:
        if row["attempts"] > 0:
            assert row["status"] == "paused", (
                f"step {row['step_id']!r} with attempts={row['attempts']} "
                f"should be paused, got {row['status']!r}"
            )


def test_dispatcher_no_op_when_experiment_already_exists(
    durable_manager: JobManager,
) -> None:
    """If the job already has an experiment id (``> 0``), the
    ``create_experiment`` step is a no-op — the dispatcher skips the
    eLabFTW call and records success.
    """
    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-existing-exp")
    job.elabftw_experiment_id = 42  # caller-supplied experiment
    _durable_record_for(durable_manager, job)
    # The durable record mirrors the in-memory job's exp id.
    durable_manager.update_experiment_id(job.job_id, 42)

    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    for _ in range(8):
        worker.run_once()

    # No new experiment was created (the mock starts with an empty
    # ``_experiments`` dict).
    assert elabftw._experiments == {}, (
        "create_experiment should have been a no-op for an existing "
        f"experiment id, but the mock recorded: {elabftw._experiments!r}"
    )
    # The create_experiment step is still ``done`` — the dispatcher
    # recorded the no-op success.
    rows = list(
        durable_manager.conn.execute(
            "SELECT status, detail FROM writeback_steps "
            "WHERE job_id = ? AND action = 'create_experiment'",
            (job.job_id,),
        )
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert "skipped create" in rows[0]["detail"]
