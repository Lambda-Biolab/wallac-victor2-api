# Controlled abort and recovery — Wallac bridge

- **Status:** operational contract
- **Date:** 2026-06-25
- **Source:** issue #5, `docs/wallac-plate-reader-integration.md`,
  `docs/automation-integrations.md`

This document defines the Wallac bridge's controlled abort behavior, recovery
semantics, and incident/rollback sequence.

## Abort sources and latency

| Source | Path | Latency | Type |
|------|------|--------|------|
| Bridge HTTP API `POST /jobs/{id}/abort` | HTTP request to bridge, forwarded to vm-agent | <1 s | Real-time software abort |
| Physical emergency stop | Hardware button / Wallac console | Immediate | Emergency stop — **not** handled by the bridge |

**All software aborts go through the bridge HTTP API** (`POST /jobs/{id}/abort`).
There is no eLabFTW polling for abort requests. The abort is forwarded to the
vm-agent's `POST /runs/{id}/abort`, which is subject to the instrument's
60-second minimum abort age.

## State machine: abort lifecycle

```
running → aborted
     ↘ failed (abort itself failed)
```

- **Abort before run starts** (accepted → aborted): No physical work was done.
  The job goes directly to `aborted`.
- **Abort during run** (running → aborted): The execution loop detects the
  abort request via `POST /jobs/{id}/abort`, calls the vm-agent abort endpoint,
  and transitions to `aborted` on success or `failed` if the instrument did not
  respond.
- **Abort after completion** (terminal state): No-op. The abort request is
  logged but does not change the state.

## Recovery on restart

The bridge tracks jobs entirely in memory (``JobManager`` in
``bridge/jobs.py``). A restart clears all tracked state — there is no
persisted job queue to recover.

After restart:

- **Active jobs before restart** are lost. The operator must re-submit them
  via ``POST /jobs`` on the bridge HTTP API.
- **The vm-agent** retains its own run history independently. The operator can
  query ``GET /runs`` on the vm-agent to inspect prior instrument runs.
- **No automatic recovery.** The bridge never guesses at prior state or
  re-executes a job after restart. Ambiguous physical work requires operator
  review and a new signed Automation Job.

The ``unknown_requires_operator_review`` state exists for jobs that reach a
partial-failure during execution (e.g. results retrieved but analysis fails);
it is assigned at runtime, not on recovery.

## Incident / rollback sequence

When a job fails, aborts, or enters an ambiguous state:

1. **Halt the run.** ``BridgeExecutor._poll_run()`` (``bridge/executor.py``)
   calls ``POST /runs/{id}/abort`` on the vm-agent. If the vm-agent returns
   425 "too early" (run younger than the 60 s minimum abort age), the executor
   retries on the next poll cycle. If the abort fails permanently, the job is
   marked ``failed`` with an event log entry.

2. **Restore a known-good state.** The Wallac Victor2 has no homing or
   voltage-restoration sequence — the carrier returns to its idle position
   when the measurement stops (or is aborted). No operator disassembly is
   required. If the instrument is in an error state, call
   ``POST /admin/reconnect`` on the vm-agent to re-establish the COM link.

3. **Write back results and mark terminal state.** The executor uploads any
   available results and artifacts to eLabFTW synchronously, then writes the
   final state (``completed``, ``failed``, ``aborted``, or
   ``unknown_requires_operator_review``). If eLabFTW write-back fails after
   measurement, the job enters ``unknown_requires_operator_review`` with the
   instrument ``run_id`` and explicit guidance not to rerun automatically.
   There is no local spool or retry queue.

4. **Mark for operator review if ambiguous.** If the bridge cannot determine
   whether the run completed (partial results, failed analysis), it sets the
   job state to ``unknown_requires_operator_review`` with a structured error
   (code, severity, human_message, operator_hint, retryable, details).

5. **Do not auto-retry.** The bridge never automatically re-executes a job
   that reached an ambiguous or failed state. The operator must submit a new
   job via ``POST /jobs`` to retry.

6. **Operator review.** The operator inspects the instrument, checks for
   partial results in the vm-agent's run history, and either:
   - Accepts the job as completed if results exist, or
   - Submits a new Automation Job via the Run Builder to re-run the assay.

## Operator-facing error shape

All errors include:

| Field | Purpose |
|---|---|
| `code` | Stable machine code (e.g., `ambiguous_state`, `aborted`, `abort_failed`) |
| `severity` | `info` / `warning` / `error` / `fatal` |
| `human_message` | Operator-readable summary |
| `operator_hint` | Suggested next action |
| `retryable` | Whether the job may be resubmitted |
| `details` | Free-form structured context (item_id, persisted_state, etc.) |

## Implementation

- **Abort endpoint:** ``bridge/bridge_app.py`` ``POST /jobs/{id}/abort`` →
  sets ``job.abort_requested`` → ``BridgeExecutor._poll_run()`` calls vm-agent
  ``POST /runs/{id}/abort`` (``bridge/executor.py``).
- **State tracking:** ``JobManager`` in ``bridge/jobs.py`` — in-memory, no
  persistence.
- **Minimum abort age:** vm-agent enforces 60 s minimum (returns 425 "too
  early" if the run is too young). The executor retries on the next poll
  cycle (``bridge/executor.py:390-415``).
- **Preflight:** every non-dry-run job performs an authenticated, read-only
  eLabFTW request before any clone, plate-map mutation, assay snapshot, or
  physical run. Success emits ``elabftw_preflight_ok``; failure emits
  ``elabftw_preflight_failed`` and rejects the job without instrument side
  effects.
- **Health probes:** ``GET /health/live`` reports process liveness;
  ``GET /health/ready`` reports worker and eLabFTW/vm-agent dependency readiness.
- **No eLabFTW polling.** Abort requests arrive via HTTP, not eLabFTW metadata.
- **Tests:** ``tests/`` cover abort during existing-protocol and
  generated-protocol execution in ``JobManager`` and ``BridgeExecutor`` tests.
