"""SQLite schema and connection helpers for the durable bridge spool.

Schema is intentionally additive — every new writeback step goes
through :func:`upsert_step` keyed on a stable idempotency token so a
duplicate HTTP request never creates a duplicate eLabFTW artifact or
section.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id              TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        execution_mode      TEXT NOT NULL,
        protocol_name       TEXT NOT NULL,
        protocol_id         INTEGER NOT NULL,
        elabftw_experiment_id INTEGER NOT NULL,
        wells_spec_json      TEXT NOT NULL,
        status              TEXT NOT NULL,
        run_id              TEXT,
        assay_prot_id       INTEGER,
        created_at          TEXT NOT NULL,
        started_at          TEXT,
        completed_at        TEXT,
        error               TEXT,
        expected_outputs    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq     INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id  TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        ts      TEXT NOT NULL,
        event   TEXT NOT NULL,
        detail  TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,    -- raw | analyzed | meta
        path        TEXT NOT NULL,
        sha256      TEXT NOT NULL,
        uploaded    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS writeback_steps (
        step_id     TEXT PRIMARY KEY,
        job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        action      TEXT NOT NULL,    -- create_experiment | upload_raw | upload_analyzed | patch_body  # noqa: E501
        idempotency TEXT NOT NULL UNIQUE,
        status      TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | paused
        detail      TEXT,
        attempts    INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS writeback_attempts (
        attempt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        step_id      TEXT NOT NULL REFERENCES writeback_steps(step_id) ON DELETE CASCADE,
        ts           TEXT NOT NULL,
        http_status  INTEGER,
        outcome      TEXT NOT NULL,    -- transient | permanent | success
        detail       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_steps_job ON writeback_steps(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_step ON writeback_attempts(step_id)",
]


def open_db(path: Path) -> sqlite3.Connection:
    """Return a SQLite connection in WAL + foreign-keys mode.

    Sets ``row_factory=sqlite3.Row`` so callers can use ``row["col"]``.
    The caller owns the connection; close it via :func:`close_db` or
    context manager.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        isolation_level=None,  # autocommit; we use explicit BEGIN where needed
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    for stmt in SCHEMA:
        conn.execute(stmt)
    return conn


def close_db(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic transaction context.

    Use ``with transaction(conn):`` for any multi-statement writeback
    step. SQLite is already in autocommit; we explicitly ``BEGIN`` so
    that nested errors roll back via ``ROLLBACK``.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def all_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def list_steps(conn: sqlite3.Connection, job_id: str) -> Iterable[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM writeback_steps WHERE job_id = ? ORDER BY step_id",
        (job_id,),
    )
