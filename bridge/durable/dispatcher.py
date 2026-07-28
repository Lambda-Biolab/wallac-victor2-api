"""Async writeback dispatcher.

The :class:`WritebackDispatcher` is the callback passed to
:class:`bridge.durable.worker.WritebackWorker`. It is intentionally
transport-aware: this is where the bridge talks to eLabFTW. The worker
itself only knows about retry timing and step outcomes.

The four canonical writeback actions are dispatched here:

* ``create_experiment`` — call eLabFTW ``POST /experiments`` (no-op
  when the job already carries an experiment id).
* ``upload_raw`` — call eLabFTW ``POST /experiments/{id}/uploads``
  with the bytes spooled under ``${STATE_DIR}/spool/<job_id>/raw.json``.
* ``upload_analyzed`` — same, for the analyzed CSV.
* ``patch_body`` — GET the experiment, merge the section, PATCH back.

The dispatcher is the only code path that performs the actual eLabFTW
writeback when the durable spool is enabled. On restart, the worker
re-reads the durable ledger, finds the pending steps, and re-dispatches
them. Idempotency comes from the planner's stable idempotency tokens
and the durable manager's ``mark_artifact_uploaded`` flag — the
dispatcher skips uploads whose artifact is already recorded as
uploaded, and the ``merge_results_section`` helper replaces only the
bracketed section so the body PATCH is safe to repeat.

Issue #44: the state machine stops at ``measured`` (hardware side).
Everything after ``measured`` flows through this dispatcher.
"""

from __future__ import annotations

import logging
import socket
import threading
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from bridge.durable.planner import merge_results_section
from bridge.durable.retry import classify_status
from bridge.durable.worker import (
    RetryAction,
    StepLedger,
    _PrerequisiteNotMet,
    record_step_outcome,
)

logger = logging.getLogger(__name__)


# Actions whose execution depends on ``create_experiment`` finishing
# first. The worker defers these steps (no retry, just push
# ``next_attempt_at`` into the past) when the experiment id is still
# zero, so they automatically pick up after the prerequisite is done.
_PREREQUISITE_ACTIONS = frozenset({"upload_raw", "upload_analyzed", "patch_body"})


# ``urllib.error.HTTPError`` is the canonical transient/permanent
# boundary marker for the real :class:`bridge.elabftw.ElabftwClient`.
# The protocol doesn't require it, but catching it explicitly lets
# the dispatcher classify network-layer failures correctly.
_HTTPError = urllib.error.HTTPError


def _classify_http_error(exc: BaseException, what: str) -> tuple[str, str]:
    """Map an ``HTTPError`` raised by eLabFTW to ``(outcome, detail)``.

    Uses :func:`bridge.durable.retry.classify_status` so the
    transient / permanent boundary stays in one place. ``detail``
    is human-readable and includes the status code + the
    exception's reason string.
    """
    code = int(getattr(exc, "code", 0) or 0)
    reason = str(getattr(exc, "reason", exc))
    outcome = classify_status(code)
    return outcome, f"{what} returned HTTP {code} ({reason})"


# Exception types that represent transient network conditions. The
# real :class:`bridge.elabftw.ElabftwClient` uses ``urllib.request``
# which surfaces DNS / connection / timeout errors through these
# classes. All of them are transient per issue #44 §"Retry policy"
# (5xx-equivalent — the eLabFTW server is unreachable, so the
# outcome should be retried with bounded backoff, not paused).
_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    urllib.error.URLError,  # parent of HTTPError; covers bad URL / DNS
    urllib.error.HTTPError,  # subclass handled separately for status
    socket.timeout,
    ConnectionError,  # base of ConnectionRefusedError, ConnectionResetError, etc.
    TimeoutError,
    OSError,  # parent of socket.gaierror, ConnectionRefusedError, etc.
)


class _ElabftwLike(Protocol):
    """Minimal eLabFTW surface used by the dispatcher.

    Both :class:`bridge.elabftw.ElabftwClient` and the in-memory
    ``MockElabftwClient`` used in tests satisfy this protocol.
    """

    def create_experiment(self, title: str, body: str = "") -> int: ...

    def get_experiment(self, experiment_id: int) -> dict[str, Any]: ...

    def patch_experiment(self, experiment_id: int, fields: dict[str, Any]) -> None: ...

    def upload_experiment_file(
        self,
        experiment_id: int,
        filename: str,
        content: bytes,
        comment: str = "",
    ) -> dict[str, Any]: ...


if TYPE_CHECKING:
    from bridge.durable.manager import JobManager


def _lock_for(
    locks: dict[int, threading.Lock], guard: threading.Lock, exp_id: int
) -> threading.Lock:
    """Return a per-experiment lock, creating it on first use.

    Mirrors :meth:`BridgeExecutor._writeback_lock_for`: the worker
    thread and the request thread (e.g. an explicit
    ``POST /writeback/<id>/retry``) must serialise their GET-merge-PATCH
    cycle on the same experiment id.
    """
    with guard:
        lock = locks.get(exp_id)
        if lock is None:
            lock = threading.Lock()
            locks[exp_id] = lock
        return lock


class WritebackDispatcher:
    """Bridge-side callback for :class:`WritebackWorker`.

    Constructor args:

    * ``manager`` — the durable :class:`bridge.durable.manager.JobManager`
      the worker reads steps from and writes outcomes to.
    * ``elabftw`` — the eLabFTW client (or a test double) used to
      actually perform the four writeback stages.
    * ``on_step_complete`` — optional ``Callable[[str, str, str], None]``
      invoked after each successful step with ``(job_id, action, exp_id)``.
      Useful for the executor to mirror durable state into the
      in-memory ``Job`` (e.g. setting ``elabftw_experiment_id``).
    * ``on_all_steps_done`` — optional ``Callable[[str], None]``
      invoked once after the last writeback step for a job
      successfully completes. The dispatcher does NOT mark the durable
      job ``completed`` itself; the caller decides the final status.
    """

    def __init__(
        self,
        manager: JobManager,
        elabftw: _ElabftwLike,
        *,
        on_step_complete: Callable[[str, str, str], None] | None = None,
        on_all_steps_done: Callable[[str], None] | None = None,
    ) -> None:
        self.manager = manager
        self.elabftw = elabftw
        self._on_step_complete = on_step_complete
        self._on_all_steps_done = on_all_steps_done
        self._ledger = StepLedger(manager.conn)
        self._body_locks: dict[int, threading.Lock] = {}
        self._body_locks_guard = threading.Lock()

    def dispatch(self, action: RetryAction) -> None:
        """Perform the eLabFTW operation for one writeback step.

        Raises any underlying HTTP/TLS/parse error so the worker can
        classify the outcome and decide whether to retry, pause, or
        leave the step pending.
        """
        job = self.manager.get_job(action.job_id)
        if job is None:
            raise RuntimeError(f"unknown job_id in writeback step: {action.job_id!r}")

        if action.action == "create_experiment":
            self._create_experiment(action, job)
        elif action.action == "upload_raw":
            self._upload(action, job, kind="raw", filename=f"{job.job_id}_raw_results.json")
        elif action.action == "upload_analyzed":
            self._upload(
                action,
                job,
                kind="analyzed",
                filename=f"{job.job_id}_analyzed.csv",
            )
        elif action.action == "patch_body":
            self._patch_body(action, job)
        else:
            raise ValueError(f"unknown writeback action: {action.action!r}")

    # --- step implementations -------------------------------------------

    def _create_experiment(self, action: RetryAction, job: Any) -> None:
        if job.elabftw_experiment_id != 0:
            # Existing experiment: this step is a no-op. Record success
            # so the worker advances.
            record_step_outcome(
                self._ledger,
                step_id=action.step_id,
                http_status=200,
                detail="experiment already exists; skipped create",
            )
            self._after_step(action.job_id, action.action, str(job.elabftw_experiment_id))
            self._maybe_finish(action.job_id)
            return
        title = f"Wallac Victor2 — {job.title}"
        body = f"<p>Results from job <code>{job.job_id}</code></p>"
        try:
            exp_id = self.elabftw.create_experiment(title, body)
        except BaseException as exc:
            self._record_transport_outcome(action, exc, "create_experiment")
            return
        self.manager.update_experiment_id(job.job_id, exp_id)
        record_step_outcome(
            self._ledger,
            step_id=action.step_id,
            http_status=201,
            detail=f"experiment_id={exp_id}",
        )
        self._after_step(action.job_id, action.action, str(exp_id))
        self._maybe_finish(action.job_id)

    def _upload(self, action: RetryAction, job: Any, *, kind: str, filename: str) -> None:
        exp_id = job.elabftw_experiment_id
        if exp_id == 0:
            # Prerequisite not yet satisfied: ``create_experiment``
            # is still pending. Defer this step (no retry, no
            # attempt increment) so the next worker tick picks it
            # up after the experiment exists.
            raise _PrerequisiteNotMet(
                f"{action.action}: create_experiment not yet done for job {job.job_id}"
            )
        artifact = self.manager.find_artifact(job.job_id, kind)
        if artifact is None:
            raise RuntimeError(f"{action.action}: no {kind} artifact spooled for job {job.job_id}")
        if artifact.uploaded:
            # Idempotent skip: the durable manager already has the
            # ``uploaded=1`` flag set, so a duplicate dispatch is a
            # no-op against the remote. The HTTP status we record is
            # 200 (no-op success).
            record_step_outcome(
                self._ledger,
                step_id=action.step_id,
                http_status=200,
                detail=f"{kind} already uploaded (sha={artifact.sha256[:12]})",
            )
            self._after_step(action.job_id, action.action, str(exp_id))
            self._maybe_finish(action.job_id)
            return
        data = Path(artifact.path).read_bytes()
        # ``elabftw.upload_experiment_file`` raises ``HTTPError`` for
        # non-2xx responses, and ``URLError``/``OSError``/``socket.timeout``
        # for transport-level failures. Catch the full transient set,
        # classify, and let the worker advance the ledger through the
        # normal outcome path so ``max_attempts`` is enforced.
        try:
            self.elabftw.upload_experiment_file(
                exp_id,
                filename,
                data,
                comment=f"{kind} results from Wallac Victor2 (job {job.job_id})",
            )
        except BaseException as exc:
            self._record_transport_outcome(action, exc, f"{kind} upload")
            return
        self.manager.mark_artifact_uploaded(job.job_id, artifact.sha256)
        record_step_outcome(
            self._ledger,
            step_id=action.step_id,
            http_status=201,
            detail=f"{kind} uploaded (sha={artifact.sha256[:12]})",
        )
        self._after_step(action.job_id, action.action, str(exp_id))
        self._maybe_finish(action.job_id)

    def _patch_body(self, action: RetryAction, job: Any) -> None:
        exp_id = job.elabftw_experiment_id
        if exp_id == 0:
            raise _PrerequisiteNotMet(
                f"patch_body: create_experiment not yet done for job {job.job_id}"
            )
        body_artifact = self.manager.find_artifact(job.job_id, "body")
        if body_artifact is None:
            raise RuntimeError(f"patch_body: no body artifact spooled for job {job.job_id}")
        section_html = Path(body_artifact.path).read_text()
        with _lock_for(self._body_locks, self._body_locks_guard, exp_id):
            try:
                existing = self.elabftw.get_experiment(exp_id)
            except BaseException as exc:
                self._record_transport_outcome(action, exc, "get_experiment")
                return
            existing_body = existing.get("body") or ""
            merged = merge_results_section(existing_body, section_html, job_id=job.job_id)
            try:
                self.elabftw.patch_experiment(exp_id, {"body": merged})
            except BaseException as exc:
                self._record_transport_outcome(action, exc, "patch_experiment")
                return
        record_step_outcome(
            self._ledger,
            step_id=action.step_id,
            http_status=200,
            detail="body merged and PATCHed",
        )
        self._after_step(action.job_id, action.action, str(exp_id))
        self._maybe_finish(action.job_id)

    # --- hooks ----------------------------------------------------------

    def _record_transport_outcome(self, action: RetryAction, exc: BaseException, what: str) -> None:
        """Classify any exception raised by an eLabFTW call and record
        the outcome through the normal ledger path.

        * ``HTTPError`` (with a status code) → ``classify_status`` →
          transient or permanent based on the code.
        * ``URLError`` / ``OSError`` / ``socket.timeout`` /
          ``ConnectionError`` / ``TimeoutError`` → transient (issue
          #44 §"Retry policy": transport failures retry with
          bounded backoff; the worker will pause the step once the
          attempt budget is exhausted).
        * Anything else → re-raise so the worker treats it as a
          permanent dispatcher bug (the existing exception handler
          in ``WritebackWorker.run_once`` pauses the step without
          incrementing the retry count further).
        """
        if isinstance(exc, _TRANSIENT_EXC):
            if isinstance(exc, _HTTPError):
                outcome, detail = _classify_http_error(exc, what)
                status = int(getattr(exc, "code", 0) or 0)
            else:
                # Non-HTTPError transport failure: DNS, connection
                # refused, timeout, etc. Classify as transient so the
                # bounded backoff handles it.
                outcome = "transient"
                detail = f"{what} transport error: {exc!r}"
                status = 0
            record_step_outcome(
                self._ledger,
                step_id=action.step_id,
                http_status=status,
                detail=detail,
            )
            logger.warning(
                "%s %s for job %s (status=%s): %s",
                action.action,
                outcome,
                action.job_id,
                status,
                detail,
            )
            return
        # Unknown exception type — let the worker treat it as a
        # permanent dispatcher bug.
        raise exc

    def _after_step(self, job_id: str, action: str, exp_id: str) -> None:
        if self._on_step_complete is not None:
            try:
                self._on_step_complete(job_id, action, exp_id)
            except Exception:
                logger.exception("on_step_complete hook raised for %s/%s", job_id, action)

    def _maybe_finish(self, job_id: str) -> None:
        """Notify the caller if every step for the job is now ``done``.

        The dispatcher intentionally does NOT mark the durable job
        ``completed`` — the caller owns the final state machine
        transition so it can also update the in-memory ``Job`` and
        emit the ``writeback_completed`` event the same way the
        legacy path did.
        """
        rows = self._ledger.snapshot(job_id)
        if not rows:
            return
        if all(r["status"] == "done" for r in rows) and self._on_all_steps_done is not None:
            try:
                self._on_all_steps_done(job_id)
            except Exception:
                logger.exception("on_all_steps_done hook raised for %s", job_id)


__all__ = ["WritebackDispatcher"]
