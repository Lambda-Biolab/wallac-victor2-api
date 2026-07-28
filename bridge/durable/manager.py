"""Durable ``JobManager`` backed by SQLite.

Mirrors the in-memory API exposed by :mod:`bridge.jobs` (the
existing ``JobManager`` + ``Job`` class). The durable manager writes
every lifecycle event and artifact row inside a single SQLite
transaction so a crash between event emission and acknowledgement
cannot leave the bridge in an inconsistent state.

Public API:

    JobManager(state_dir=Path("/var/lib/wallac-bridge"))
    manager.submit_job({...}) -> Job
    manager.get_job(job_id) -> Job | None
    manager.list_jobs() -> list[Job]
    manager.record_event(job_id, event, detail)
    manager.record_artifact(job_id, kind, path, sha256)
    manager.mark_status(job_id, status, error=None)
    manager.request_abort(job_id) -> bool
    manager.snapshot() -> dict         — used by the operator endpoints

The ``Job`` returned is a value object reconstructed from the row; mutating
it does not persist. Use :meth:`mark_status` and :meth:`record_event`
to apply changes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bridge.durable.schema import close_db, open_db, transaction


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f%z")


@dataclass
class Artifact:
    kind: str  # raw | analyzed | meta
    path: str
    sha256: str
    uploaded: bool = False


@dataclass
class Job:
    job_id: str
    title: str
    execution_mode: str
    protocol_name: str
    protocol_id: int
    elabftw_experiment_id: int
    wells_spec: dict[str, Any]
    status: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    run_id: str | None = None
    assay_prot_id: int | None = None
    error: str | None = None
    expected_outputs: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "execution_mode": self.execution_mode,
            "protocol_name": self.protocol_name,
            "protocol_id": self.protocol_id,
            "elabftw_experiment_id": self.elabftw_experiment_id,
            "wells_spec": self.wells_spec,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "run_id": self.run_id,
            "assay_prot_id": self.assay_prot_id,
            "error": self.error,
            "events": list(self.events),
            "artifacts": [
                {
                    "kind": a.kind,
                    "path": a.path,
                    "sha256": a.sha256,
                    "uploaded": a.uploaded,
                }
                for a in self.artifacts
            ],
            "expected_outputs": self.expected_outputs,
        }


class JobManager:
    """Durable job ledger.

    The manager owns a single SQLite connection. Callers that need
    concurrent writes (e.g. the worker thread plus the request thread)
    must serialise through :class:`bridge.executor.WritebackLock`
    or the bridge-level per-experiment lock; this class itself does
    not lock — it relies on SQLite's IMMEDIATE transactions to give
    callers single-writer semantics when paired with :func:`transaction`.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = state_dir / "bridge.sqlite3"
        self.conn = open_db(self.db_path)

    def close(self) -> None:
        close_db(self.conn)

    # --- submission ----------------------------------------------------

    def submit_job(
        self,
        *,
        job_id: str,
        title: str,
        execution_mode: str,
        protocol_name: str,
        protocol_id: int,
        elabftw_experiment_id: int,
        wells_spec: dict[str, Any],
        expected_outputs: str = "",
    ) -> Job:
        now = _now_iso()
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO jobs (
                    job_id, title, execution_mode, protocol_name, protocol_id,
                    elabftw_experiment_id, wells_spec_json, status, created_at,
                    expected_outputs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    job_id,
                    title,
                    execution_mode,
                    protocol_name,
                    protocol_id,
                    elabftw_experiment_id,
                    json.dumps(wells_spec, sort_keys=True),
                    now,
                    expected_outputs,
                ),
            )
            self._add_event(job_id, "job_submitted", "")
        return self.get_job(job_id)  # type: ignore[return-value]

    # --- reads ----------------------------------------------------------

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._row_to_job(r) for r in rows]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [
            {"ts": r["ts"], "event": r["event"], "detail": r["detail"]}
            for r in self.conn.execute(
                "SELECT ts, event, detail FROM events WHERE job_id = ? ORDER BY seq",
                (job_id,),
            )
        ]

    def artifact_rows(self, job_id: str) -> list[Artifact]:
        return [
            Artifact(
                kind=r["kind"],
                path=r["path"],
                sha256=r["sha256"],
                uploaded=bool(r["uploaded"]),
            )
            for r in self.conn.execute(
                "SELECT kind, path, sha256, uploaded FROM artifacts "
                "WHERE job_id = ? ORDER BY artifact_id",
                (job_id,),
            )
        ]

    # --- writes ---------------------------------------------------------

    def mark_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        run_id: str | None = None,
        assay_prot_id: int | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with transaction(self.conn):
            sets: list[str] = ["status = ?"]
            params: list[Any] = [status]
            if error is not None:
                sets.append("error = ?")
                params.append(error)
            if run_id is not None:
                sets.append("run_id = ?")
                params.append(run_id)
            if assay_prot_id is not None:
                sets.append("assay_prot_id = ?")
                params.append(assay_prot_id)
            if started_at is not None:
                sets.append("started_at = ?")
                params.append(started_at)
            if completed_at is not None:
                sets.append("completed_at = ?")
                params.append(completed_at)
            params.append(job_id)
            # ``sets`` is built only from hardcoded column names; values
            # are bound via the ``?`` placeholders.
            self.conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?",  # noqa: S608
                tuple(params),
            )

    def record_event(self, job_id: str, event: str, detail: str = "") -> None:
        with transaction(self.conn):
            self._add_event(job_id, event, detail)

    def record_artifact(
        self,
        job_id: str,
        kind: str,
        path: str,
        sha256: str,
        uploaded: bool = False,
    ) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO artifacts (job_id, kind, path, sha256, uploaded)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, kind, path, sha256, 1 if uploaded else 0),
            )

    def mark_artifact_uploaded(self, job_id: str, sha256: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE artifacts SET uploaded = 1 WHERE job_id = ? AND sha256 = ?",
                (job_id, sha256),
            )

    def request_abort(self, job_id: str) -> bool:
        """Mark a job aborted before physical execution begins.

        Once a job has reached ``running``, the abort path lives in
        the bridge worker (issue #22 / slice-2). This helper handles
        only the pre-execution abort: an accepted job that has not yet
        been picked up by the worker.
        """
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] not in ("accepted", "preflight_passed"):
                return False
            self.conn.execute(
                "UPDATE jobs SET status = 'aborted', completed_at = ? WHERE job_id = ?",
                (_now_iso(), job_id),
            )
            self._add_event(job_id, "aborted_pre_execution", "")
        return True

    # --- helpers --------------------------------------------------------

    def _add_event(self, job_id: str, event: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, ts, event, detail) VALUES (?, ?, ?, ?)",
            (job_id, _now_iso(), event, detail),
        )

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            title=row["title"],
            execution_mode=row["execution_mode"],
            protocol_name=row["protocol_name"],
            protocol_id=row["protocol_id"],
            elabftw_experiment_id=row["elabftw_experiment_id"],
            wells_spec=json.loads(row["wells_spec_json"]),
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            run_id=row["run_id"],
            assay_prot_id=row["assay_prot_id"],
            error=row["error"],
            expected_outputs=row["expected_outputs"] or "",
            events=self.events(row["job_id"]),
            artifacts=self.artifact_rows(row["job_id"]),
        )

    # --- snapshot for operator endpoints -------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Operator-facing view of the ledger.

        Used by ``GET /writeback`` and the recovery endpoints. The
        shape is intentionally minimal: status, error, experiment ID,
        artifact count, event count, oldest pending step.
        """
        rows = self.conn.execute(
            """
            SELECT job_id, status, error, elabftw_experiment_id,
                   (SELECT COUNT(*) FROM artifacts WHERE job_id = jobs.job_id) AS n_artifacts,
                   (SELECT COUNT(*) FROM events WHERE job_id = jobs.job_id) AS n_events,
                   created_at, completed_at, run_id
              FROM jobs
              ORDER BY created_at DESC
            """
        ).fetchall()
        oldest = self.conn.execute(
            """
            SELECT step_id, job_id, attempts, next_attempt_at
              FROM writeback_steps
              WHERE status = 'pending'
              ORDER BY COALESCE(next_attempt_at, '') ASC
              LIMIT 1
            """
        ).fetchone()
        return {
            "jobs": [dict(r) for r in rows],
            "oldest_pending_step": dict(oldest) if oldest is not None else None,
        }
