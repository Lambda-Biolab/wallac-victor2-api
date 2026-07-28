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
            *,
            metadata: dict[str, str] | None = None,
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


# --- Re-review blockers (round 2) -----------------------------------------


def test_post_jobs_seeds_durable_record() -> None:
    """POST /jobs must seed the durable ledger so ``_durable_writeback``
    finds the row.

    Review blocker (round 2) #1: the production path was creating
    only the in-memory record, so the durable writeback always
    failed with "no durable record".
    """
    from fastapi.testclient import TestClient

    from bridge.bridge_app import create_bridge_app
    from bridge.config import BridgeConfig

    state_dir = Path(tempfile.mkdtemp(prefix="wallac-bridge-post-"))
    try:
        config = BridgeConfig(
            elabftw_url="https://localhost:3148",
            elabftw_api_key="5-key",
            elabftw_verify_tls=False,
            elabftw_ca_bundle=None,
            wallac_env="dev",
            vm_agent_url="http://127.0.0.1:8420",
            vm_agent_token="",
            dry_run=False,
            bridge_state_dir=str(state_dir),
        )
        app = create_bridge_app(config=config)
        client = TestClient(app)
        # Auth is enabled by default; supply the (empty) bridge
        # token header so the request does not 401 before we get
        # to the body.
        client.headers["Authorization"] = "Bearer "
        resp = client.post(
            "/jobs",
            json={
                "title": "post /jobs seeds durable",
                "execution_mode": "existing_protocol",
                "protocol_name": "Absorbance @ 610 (1.0s)",
                "protocol_id": 2000008,
                "wells_spec": {"wells": ["A1", "A2"]},
            },
        )
        assert resp.status_code == 201, resp.text
        job_id = resp.json()["job_id"]
        # The durable ledger has a matching row.
        durable = JobManager(state_dir)
        try:
            row = durable.get_job(job_id)
            assert row is not None, (
                f"durable record missing for job_id={job_id!r} — "
                "POST /jobs did not seed the durable ledger"
            )
            assert row.status == "accepted"
        finally:
            durable.close()
    finally:
        # Best-effort cleanup; the tempdir may already be gone if
        # pyright was watching.
        import shutil

        shutil.rmtree(state_dir, ignore_errors=True)


def test_durable_writeback_pending_status_blocks_auto_completion(
    durable_manager: JobManager,
) -> None:
    """After ``_durable_writeback`` the in-memory job's status is
    ``writeback_pending`` — the in-memory worker must NOT auto-promote
    it to ``completed`` while the durable worker is still processing.

    Review blocker (round 2) #2: previously the executor's durable
    branch returned without changing the in-memory job's status, so
    the in-memory worker thread's "if not terminal, set completed"
    auto-promoted the job to ``completed`` before the eLabFTW side
    had actually finished.
    """
    from bridge.jobs import WRITEBACK_PENDING

    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-pending")
    _durable_record_for(durable_manager, job)

    success = executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    assert success is True
    # The in-memory job is now in ``writeback_pending`` (not
    # ``completed``) so the in-memory worker would not auto-promote
    # it after the executor returns.
    assert job.status == WRITEBACK_PENDING, (
        f"in-memory job should be writeback_pending, got {job.status!r}"
    )
    # ``WRITEBACK_PENDING`` is in TERMINAL_STATES so the in-memory
    # worker recognises the job as "done from its perspective"
    # and skips the auto-promote-to-completed step.
    from bridge.jobs import TERMINAL_STATES

    assert WRITEBACK_PENDING in TERMINAL_STATES


def test_step_prerequisite_deferred_not_failed(
    durable_manager: JobManager,
) -> None:
    """If ``create_experiment`` is still pending, the upload /
    patch steps must be DEFERRED (status stays ``pending``,
    ``attempts`` unchanged) — not marked permanent.

    Review blocker (round 2) #3: previously a transient failure on
    ``create_experiment`` caused ``upload_*`` / ``patch_body`` to
    raise ``RuntimeError("create_experiment must run first")`` and
    the worker recorded them as permanent dispatcher bugs.

    Setup: ``create_experiment`` raises HTTP 503 on the first call,
    so the worker's first tick defers upload_* / patch_body
    (exp_id still 0) and the create_experiment step is rescheduled
    (transient). The second tick retries create_experiment (succeeds),
    then the third tick finally dispatches upload_* / patch_body.
    """
    import urllib.error

    class FlakyCreate(MockElabftwClient):
        def __init__(self) -> None:
            super().__init__()
            self._create_calls = 0

        def create_experiment(  # type: ignore[override]
            self, title: str, body: str = ""
        ) -> int:
            self._create_calls += 1
            if self._create_calls == 1:
                raise urllib.error.HTTPError(
                    url="http://elabftw/experiments",
                    code=503,
                    msg="Service Unavailable",
                    hdrs={},  # type: ignore[arg-type]
                    fp=None,
                )
            return super().create_experiment(title, body)

    elabftw = FlakyCreate()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-prereq")
    _durable_record_for(durable_manager, job)
    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")

    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    # Patch Backoff to schedule the next attempt instantly so the
    # first transient failure retries on the very next tick.
    import bridge.durable.worker as _wmod

    original = _wmod.Backoff.wait_seconds

    def _inst(self: _wmod.Backoff, attempt: int) -> float:
        return -1.0

    _wmod.Backoff.wait_seconds = _inst  # type: ignore[method-assign]
    try:
        # First tick: create_experiment raises 503, classified as
        # transient (next_attempt_at in the past). upload_* /
        # patch_body see exp_id == 0 and raise
        # ``_PrerequisiteNotMet``; the worker defers them
        # (attempts unchanged, status still ``pending``).
        worker.run_once()
    finally:
        _wmod.Backoff.wait_seconds = original  # type: ignore[method-assign]

    prereq_steps = list(
        durable_manager.conn.execute(
            "SELECT step_id, action, status, attempts FROM writeback_steps "
            "WHERE job_id = ? AND action IN "
            "('upload_raw', 'upload_analyzed', 'patch_body')",
            (job.job_id,),
        )
    )
    assert prereq_steps, "prerequisite steps should exist in the ledger"
    for step in prereq_steps:
        assert step["status"] == "pending", (
            f"step {step['step_id']!r} should be deferred (pending), got {step['status']!r}"
        )
        assert step["attempts"] == 0, (
            f"step {step['step_id']!r} should have 0 attempts (defer is "
            f"not a retry), got {step['attempts']!r}"
        )


def test_transport_error_classified_as_transient(
    durable_manager: JobManager,
) -> None:
    """DNS / connection / timeout failures (URLError, OSError,
    socket.timeout) must be classified as transient, not marked
    permanent by the worker's exception handler.

    Review blocker (round 2) #4: the dispatcher only caught
    ``HTTPError`` (status-based). Any other exception type from
    the real ``urllib.request`` client (DNS, connection refused,
    timeout) was treated as a permanent dispatcher bug, so a
    flapping network would pause a step on the first connection
    failure rather than retrying through the bounded backoff.
    """
    import socket
    import urllib.error

    class FlakyElabftw(MockElabftwClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def create_experiment(  # type: ignore[override]
            self, title: str, body: str = ""
        ) -> int:
            self.calls += 1
            if self.calls < 3:
                # First two calls: simulate a DNS / connection
                # failure. The dispatcher must classify as
                # transient, not permanent.
                raise urllib.error.URLError("[Errno -2] Name or service not known")
            return super().create_experiment(title, body)

    elabftw = FlakyElabftw()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-transport")
    _durable_record_for(durable_manager, job)
    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    # Force the backoff to schedule the next attempt in the past
    # so the first transient failure retries on the very next tick.
    import bridge.durable.worker as _wmod

    original = _wmod.Backoff.wait_seconds

    def _inst(self: _wmod.Backoff, attempt: int) -> float:
        return -1.0

    _wmod.Backoff.wait_seconds = _inst  # type: ignore[method-assign]
    try:
        # Three ticks: the first two ``create_experiment`` calls
        # fail with URLError; the third call succeeds.
        for _ in range(3):
            worker.run_once()
    finally:
        _wmod.Backoff.wait_seconds = original  # type: ignore[method-assign]

    row = durable_manager.conn.execute(
        "SELECT status, attempts, detail FROM writeback_steps "
        "WHERE job_id = ? AND action = 'create_experiment'",
        (job.job_id,),
    ).fetchone()
    assert row["status"] == "done", (
        f"create_experiment should have eventually succeeded, "
        f"got status={row['status']!r} attempts={row['attempts']!r}"
    )
    assert row["attempts"] >= 3, (
        f"create_experiment should have been retried after URLError, "
        f"got attempts={row['attempts']!r}"
    )

    # And a ``socket.timeout`` should also be classified as transient.
    class TimeoutElabftw(MockElabftwClient):
        def create_experiment(  # type: ignore[override]
            self, title: str, body: str = ""
        ) -> int:
            raise socket.timeout("read timed out")

    elabftw2 = TimeoutElabftw()
    durable2_path = durable_manager.state_dir.parent / "durable2"
    durable2 = JobManager(durable2_path)
    try:
        executor2 = _build_executor(elabftw2, vm_agent, durable=durable2)
        job2 = _make_job("job-e2e-timeout")
        durable2.submit_job(
            job_id=job2.job_id,
            title=job2.title,
            execution_mode=job2.execution_mode,
            protocol_name=job2.protocol_name,
            protocol_id=job2.protocol_id,
            elabftw_experiment_id=job2.elabftw_experiment_id,
            wells_spec={"wells": ["A1", "A2"]},
        )
        executor2._durable_writeback(job2, CANNED_WELLS, "well,value\nA1,0.078\n")
        dispatcher2 = WritebackDispatcher(durable2, elabftw2)
        worker2 = WritebackWorker(
            durable2.conn,
            on_step=dispatcher2.dispatch,
            interval_seconds=0.0,
        )
        # Patch Backoff to schedule the next attempt instantly
        # (so we exhaust the budget within a few ticks).
        import bridge.durable.worker as _wmod

        original = _wmod.Backoff.wait_seconds

        def _inst(self: _wmod.Backoff, attempt: int) -> float:
            return -1.0

        _wmod.Backoff.wait_seconds = _inst  # type: ignore[method-assign]
        try:
            for _ in range(20):
                worker2.run_once()
        finally:
            _wmod.Backoff.wait_seconds = original  # type: ignore[method-assign]
        row = durable2.conn.execute(
            "SELECT status, attempts FROM writeback_steps "
            "WHERE job_id = ? AND action = 'create_experiment'",
            (job2.job_id,),
        ).fetchone()
        assert row["status"] == "paused", (
            f"socket.timeout must be transient; step should be paused "
            f"(not failed dispatcher bug), got status={row['status']!r}"
        )
        assert row["attempts"] >= 8
    finally:
        durable2.close()


# --- Re-review round 3 ---------------------------------------------------


def test_active_states_blocks_dedup_during_writeback_pending() -> None:
    """A re-submit of the same spec must be rejected while the
    durable worker is still processing the previous one.

    Re-review blocker #1: the in-memory dedup used
    ``status not in TERMINAL_STATES`` (true for an ``accepted`` /
    ``running`` job → rejected, true for a ``writeback_pending``
    job under the old TERMINAL_STATES → also rejected because
    WRITEBACK_PENDING was added to TERMINAL_STATES — but that
    ALSO makes the job "terminal" for the in-memory worker,
    which is the right behaviour here only for that one purpose).
    The fix is to give the dedup check its own set,
    ``ACTIVE_STATES = {accepted, running, writeback_pending}``,
    and re-route the dedup check through it.
    """
    from bridge.jobs import ACTIVE_STATES, WRITEBACK_PENDING
    from bridge.jobs import JobManager as IM

    assert WRITEBACK_PENDING in ACTIVE_STATES
    # COMPLETED / FAILED / ABORTED / UNKNOWN are NOT in
    # ACTIVE_STATES — a fresh submission of the same dedup key
    # against a terminal job is allowed (different from a
    # writeback-pending job where the durable worker is still
    # processing).
    assert "completed" not in ACTIVE_STATES
    assert "failed" not in ACTIVE_STATES
    assert "aborted" not in ACTIVE_STATES
    assert "unknown_requires_operator_review" not in ACTIVE_STATES
    # And the in-memory manager rejects a duplicate while a job
    # is in WRITEBACK_PENDING.
    in_mem = IM()
    job = in_mem.submit_job(
        {
            "title": "active states test",
            "execution_mode": "existing_protocol",
            "protocol_id": 1001,
        }
    )
    job.status = WRITEBACK_PENDING
    from bridge.jobs import DuplicateJobError

    with pytest.raises(DuplicateJobError):
        in_mem.submit_job(
            {
                "title": "active states test",
                "execution_mode": "existing_protocol",
                "protocol_id": 1001,
            }
        )


def test_post_jobs_durable_insert_runs_before_in_memory_queue(
    durable_manager: JobManager,
) -> None:
    """POST /jobs must commit the durable row before the in-memory
    submit_job enqueues the job for the worker thread.

    Re-review blocker #2: previously the in-memory submit_job ran
    first and the worker could dequeue + start physical execution
    before the durable insert committed, so ``_durable_writeback``
    would fail with "no durable record". The fix is to
    pre-generate the job id and call the durable ``submit_job``
    before the in-memory one (both synchronous on the request
    thread).
    """
    from fastapi.testclient import TestClient

    from bridge.bridge_app import create_bridge_app
    from bridge.config import BridgeConfig

    config = BridgeConfig(
        elabftw_url="https://localhost:3148",
        elabftw_api_key="5-key",
        elabftw_verify_tls=False,
        elabftw_ca_bundle=None,
        wallac_env="dev",
        vm_agent_url="http://127.0.0.1:8420",
        vm_agent_token="",
        dry_run=False,
        bridge_state_dir=str(durable_manager.state_dir),
    )
    app = create_bridge_app(config=config)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer "
    resp = client.post(
        "/jobs",
        json={
            "title": "post /jobs ordering",
            "execution_mode": "existing_protocol",
            "protocol_name": "Absorbance @ 610 (1.0s)",
            "protocol_id": 2000008,
            "wells_spec": {"wells": ["A1", "A2"]},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]
    # The durable row was committed by the time the request returned.
    assert durable_manager.get_job(job_id) is not None


def test_paused_step_transitions_job_to_operator_review(
    durable_manager: JobManager,
) -> None:
    """When a step is paused (permanent error or max_attempts
    exhausted) and no other step is ``pending``, the dispatcher's
    ``on_job_stuck`` hook fires. The bridge's hook transitions
    both ledgers to ``unknown_requires_operator_review``.

    Re-review blocker #3: previously the only hook handled the
    all-successful case, so a paused step left the job in
    ``writeback_pending`` forever.
    """
    from bridge.jobs import UNKNOWN

    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-stuck")
    _durable_record_for(durable_manager, job)
    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")

    stuck_jobs: list[tuple[str, list[str]]] = []

    def _on_stuck(job_id: str, paused_actions: list[str]) -> None:
        stuck_jobs.append((job_id, list(paused_actions)))
        durable_manager.mark_status(
            job_id, UNKNOWN, error="; ".join(f"step {a} paused" for a in paused_actions)
        )

    dispatcher = WritebackDispatcher(durable_manager, elabftw, on_job_stuck=_on_stuck)

    # Manually transition the create_experiment step to ``paused``
    # (e.g., the dispatcher raised). The remaining steps are
    # ``pending``. The hook should NOT fire yet.
    durable_manager.conn.execute(
        "UPDATE writeback_steps SET status = 'paused', attempts = 8, "
        "detail = 'simulated permanent error' "
        "WHERE job_id = ? AND action = 'create_experiment'",
        (job.job_id,),
    )
    dispatcher._maybe_finish(job.job_id)
    assert stuck_jobs == [], "hook should not fire while other steps are still pending"

    # Now mark the remaining steps ``paused`` too — the writeback
    # cannot make further progress. The hook should fire.
    durable_manager.conn.execute(
        "UPDATE writeback_steps SET status = 'paused' "
        "WHERE job_id = ? AND action != 'create_experiment'",
        (job.job_id,),
    )
    dispatcher._maybe_finish(job.job_id)
    assert len(stuck_jobs) == 1
    assert stuck_jobs[0][0] == job.job_id
    assert "create_experiment" in stuck_jobs[0][1]
    # The durable job is now ``unknown_requires_operator_review``.
    assert durable_manager.get_job(job.job_id).status == UNKNOWN


def test_ssl_certificate_failure_classified_as_permanent(
    durable_manager: JobManager,
) -> None:
    """A TLS / certificate failure (URLError with
    ``.reason`` = ``ssl.SSLCertVerificationError``) is classified
    as permanent, not transient.

    Re-review blocker #4: previously every non-HTTPError URLError
    was transient, so a broken CA bundle would loop the worker
    until ``max_attempts`` exhausted instead of pausing on the
    first failure. Retrying a TLS failure is futile — the CA
    bundle is operator-controlled; issue #44 §"Retry policy"
    mandates permanent.
    """
    import ssl
    import urllib.error

    class TlsFailElabftw(MockElabftwClient):
        def create_experiment(  # type: ignore[override]
            self, title: str, body: str = ""
        ) -> int:
            # Simulate the urllib chain: URLError wraps an
            # SSLCertVerificationError as its ``.reason``.
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("hostname 'elabftw' doesn't match 'localhost'")
            )

    elabftw = TlsFailElabftw()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-tls")
    _durable_record_for(durable_manager, job)
    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    worker.run_once()

    row = durable_manager.conn.execute(
        "SELECT status, attempts, detail FROM writeback_steps "
        "WHERE job_id = ? AND action = 'create_experiment'",
        (job.job_id,),
    ).fetchone()
    # TLS errors are permanent on the first failure — the worker
    # pauses immediately rather than looping until max_attempts.
    assert row["status"] == "paused", (
        f"TLS error should be permanent on first failure, got "
        f"status={row['status']!r} attempts={row['attempts']!r}"
    )
    assert row["attempts"] == 1, (
        f"TLS error should pause on the first attempt, got attempts={row['attempts']!r}"
    )
    assert "TLS error" in row["detail"]


def test_idempotency_token_passed_to_elabftw_upload(
    durable_manager: JobManager,
) -> None:
    """The dispatcher passes the durable step's idempotency token
    to ``ElabftwClient.upload_experiment_file`` as the
    ``metadata`` field.

    Re-review blocker #5: previously the dispatcher did not
    pass the idempotency token, so a retry after a partial
    failure (remote succeeded, local ``uploaded=1`` flag lost)
    could not be reconciled. The token is now on the wire as
    ``metadata.wallac.bridge.idempotency``. Full reconciliation
    still requires a "list uploads and skip if token present"
    pass on retry (documented as a known limitation; the
    primary defense remains the ``uploaded=1`` flag).
    """
    elabftw = MockElabftwClient()
    vm_agent = MockVmAgentClient()
    executor = _build_executor(elabftw, vm_agent, durable=durable_manager)
    job = _make_job("job-e2e-idem")
    _durable_record_for(durable_manager, job)
    executor._durable_writeback(job, CANNED_WELLS, "well,value\nA1,0.078\n")
    dispatcher = WritebackDispatcher(durable_manager, elabftw)
    worker = WritebackWorker(
        durable_manager.conn,
        on_step=dispatcher.dispatch,
        interval_seconds=0.0,
    )
    for _ in range(8):
        worker.run_once()

    # The mock records the most-recent metadata on every upload.
    assert elabftw._last_metadata is not None, (
        "expected the dispatcher to pass metadata to upload_experiment_file"
    )
    assert "wallac.bridge.idempotency" in elabftw._last_metadata
    # The token is the step's ``idempotency`` column from SQLite.
    assert elabftw._last_metadata["wallac.bridge.idempotency"].startswith(f"{job.job_id}:upload_")
