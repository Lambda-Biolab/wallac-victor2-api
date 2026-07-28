"""Background retry worker for paused writeback steps.

The worker walks the durable ledger every tick, picks up stages whose
``next_attempt_at`` is in the past, and re-enqueues them via the
executor. Permanent failures are recorded as ``operator_review`` and
never auto-retried. Transient failures follow the bounded exponential
backoff from :mod:`.retry`.

Issue #44 requires:

* Pause, do not retry, on TLS chain / hostname failure.
* Pause, do not retry, on invalid/unreadable CA bundle.
* Pause, do not retry, on HTTP 401/403.
* Pause, do not retry, on schema/payload errors.
* Pause, do not retry, on conflicting remote state.
* Bound retries on 408/425/429/5xx.

The worker thread is small and tolerant of restarts: it reads the
ledger each tick, so a process kill leaves the next attempt's
``next_attempt_at`` value unchanged and the next process picks up
where it stopped.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bridge.durable.planner import WRITEBACK_ACTIONS
from bridge.durable.retry import Backoff, classify_status
from bridge.durable.schema import transaction

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f%z")


@dataclass(frozen=True)
class RetryAction:
    """What the worker hands back to the executor.

    The executor's ``enqueue_step`` callback turns this into the
    actual HTTP call. The worker is intentionally transport-free.
    """

    step_id: str
    job_id: str
    action: str
    attempts: int


# ---------------------------------------------------------------------------
# Writeback steps ledger helpers
# ---------------------------------------------------------------------------


class StepLedger:
    """SQLite-backed writeback-step ledger.

    Splits out from :class:`JobManager` so the worker only opens the
    rows it needs (steps + attempts) and avoids contention with the
    request thread on the jobs table.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(self, steps: list[PendingStep]) -> None:
        """Insert each step as ``pending``. Idempotent on step_id."""
        with transaction(self.conn):
            for step in steps:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO writeback_steps
                      (step_id, job_id, action, idempotency, status)
                    VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (step.step_id, step.job_id, step.action, step.idempotency),
                )

    def mark_done(self, step_id: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE writeback_steps SET status = 'done', completed_at = ? WHERE step_id = ?",
                (_now_iso(), step_id),
            )

    def record_outcome(
        self,
        step_id: str,
        *,
        outcome: str,
        http_status: int | None,
        detail: str,
    ) -> tuple[str, str | None]:
        """Record an attempt outcome, return ``(status, next_attempt_at)``.

        ``status`` is one of: ``done``, ``transient``, ``permanent``.
        ``next_attempt_at`` is ISO-8601 UTC or ``None`` (no retry).
        """
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT attempts FROM writeback_steps WHERE step_id = ?",
                (step_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown step_id: {step_id!r}")
            attempts = int(row["attempts"]) + 1
            self.conn.execute(
                "INSERT INTO writeback_attempts "
                "(step_id, ts, http_status, outcome, detail) VALUES (?, ?, ?, ?, ?)",
                (step_id, _now_iso(), http_status, outcome, detail),
            )
            if outcome == "success":
                self.conn.execute(
                    "UPDATE writeback_steps SET status = 'done', "
                    "completed_at = ?, attempts = ?, detail = ? WHERE step_id = ?",
                    (_now_iso(), attempts, detail, step_id),
                )
                return ("done", None)
            if outcome == "permanent":
                self.conn.execute(
                    "UPDATE writeback_steps SET status = 'paused', "
                    "attempts = ?, detail = ? WHERE step_id = ?",
                    (attempts, detail, step_id),
                )
                return ("paused", None)

            # transient: schedule next attempt — but if the attempt
            # budget is exhausted, pause so the worker stops retrying
            # forever. The operator can explicitly /retry from the
            # recovery bundle to resume.
            backoff = Backoff()
            if attempts >= backoff.max_attempts:
                self.conn.execute(
                    "UPDATE writeback_steps SET status = 'paused', "
                    "attempts = ?, detail = ? WHERE step_id = ?",
                    (
                        attempts,
                        f"{detail}; attempt budget exhausted ({attempts}/{backoff.max_attempts})",
                        step_id,
                    ),
                )
                logger.warning(
                    "step %s paused after %d attempts (budget exhausted)",
                    step_id,
                    attempts,
                )
                return ("paused", None)
            wait = backoff.wait_seconds(attempts - 1)
            self.conn.execute(
                "UPDATE writeback_steps SET status = 'pending', "
                "attempts = ?, next_attempt_at = ?, detail = ? "
                "WHERE step_id = ?",
                (attempts, _in_future(wait), detail, step_id),
            )
            return ("pending", _in_future(wait))

    def due_steps(self, now_iso: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT step_id, job_id, action, attempts
                  FROM writeback_steps
                  WHERE status = 'pending'
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  ORDER BY COALESCE(next_attempt_at, '') ASC
                """,
                (now_iso,),
            )
        )

    def snapshot(self, job_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM writeback_steps WHERE job_id = ? ORDER BY step_id",
                (job_id,),
            )
        )


@dataclass(frozen=True)
class PendingStep:
    step_id: str
    job_id: str
    action: str
    idempotency: str


def _in_future(seconds: float) -> str:
    from datetime import timedelta

    ts = datetime.now(UTC) + timedelta(seconds=seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f%z")


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


class WritebackWorker:
    """Background retry loop.

    Usage::

        worker = WritebackWorker(
            conn=manager.conn,
            on_step=lambda action: executor.execute_step(action),
            interval_seconds=15,
        )
        worker.start()
        ...
        worker.stop()

    ``on_step`` is the bridge-specific dispatcher. The worker itself
    does not know how to perform a writeback step — it only knows how
    to retry the dispatch.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        on_step: Callable[[RetryAction], Any],
        interval_seconds: float = 15.0,
    ) -> None:
        self.ledger = StepLedger(conn)
        self._on_step = on_step
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="wallac-bridge-writeback-worker", daemon=True
        )
        self._thread.start()

    def stop(self, *, join: bool = True) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def run_once(self) -> int:
        """Process one batch of due steps. Returns count dispatched."""
        now = _now_iso()
        rows = self.ledger.due_steps(now)
        dispatched = 0
        for row in rows:
            action = RetryAction(
                step_id=row["step_id"],
                job_id=row["job_id"],
                action=row["action"],
                attempts=int(row["attempts"]),
            )
            try:
                self._on_step(action)
                dispatched += 1
            except Exception as exc:
                # A dispatcher exception is a bug, not a transient
                # network failure. Mark the step as a permanent
                # dispatcher failure so the next tick does not
                # re-attempt it every ``interval_seconds`` (the
                # previous behaviour looped the same exception
                # indefinitely, hiding the bug from operators). The
                # operator can /retry from the recovery bundle once
                # the underlying bug is fixed.
                self.ledger.record_outcome(
                    action.step_id,
                    outcome="permanent",
                    http_status=None,
                    detail=f"dispatcher raised: {exc!r}",
                )
                logger.exception(
                    "writeback dispatch raised; step %s paused",
                    action.step_id,
                )
        return dispatched

    def _loop(self) -> None:
        while not self._stop.is_set():
            # The loop must never die on transient errors. ``run_once``
            # already catches per-step exceptions, so this only fires
            # on a ledger-level error (e.g., DB locked).
            with contextlib.suppress(Exception):
                self.run_once()
            self._stop.wait(self._interval)


# ---------------------------------------------------------------------------
# Outcome classifier for the executor's dispatcher
# ---------------------------------------------------------------------------


def record_step_outcome(
    ledger: StepLedger,
    *,
    step_id: str,
    http_status: int | None,
    detail: str,
    tls_error: bool = False,
) -> tuple[str, str | None]:
    """Convenience wrapper for the executor's per-step result handler."""
    outcome = classify_status(http_status, tls_error=tls_error)
    return ledger.record_outcome(step_id, outcome=outcome, http_status=http_status, detail=detail)


__all__ = [
    "WRITEBACK_ACTIONS",
    "PendingStep",
    "RetryAction",
    "StepLedger",
    "WritebackWorker",
    "record_step_outcome",
]
