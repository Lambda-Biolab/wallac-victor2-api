"""Durable execution ledger and idempotent writeback outbox for the Wallac
bridge.

Mirror target for ``Lambda-Biolab/wallac-victor2-api#44``: a SQLite-backed
``JobManager`` replaces the in-memory dict on disk, every writeback stage
is persisted before delivery, the retry worker follows bounded exponential
backoff with jitter, and TLS/auth failures always pause for operator
action (never disable verification).

State machine (issue #44):

    accepted → preflight_passed → running → measured
        ↘ writeback_pending → writeback_partial → completed
            ↘ unknown_requires_operator_review (paused on permanent error)

Once a job reaches ``measured``, no automated path may start the hardware
again. Recovery may only resume result delivery.

Data model (see :class:`schema` for the SQL):

    jobs                 one row per bridge job; lifecycle + meta
    writeback_steps      one row per eLabFTW step; idempotency key per step
    artifacts            raw / analyzed files spooled under STATE_DIR/spool
    writeback_attempts   bounded backoff schedule per step

Spool layout::

    ${STATE_DIR}/bridge.sqlite3     SQLite ledger (WAL mode)
    ${STATE_DIR}/spool/<job_id>/    raw.json, analyzed.csv, attachment-meta.json

Service account owns STATE_DIR; umask 077 at startup.
"""
