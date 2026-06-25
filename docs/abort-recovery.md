# Controlled abort and recovery — Wallac bridge

- **Status:** operational contract
- **Date:** 2026-06-25
- **Source:** issue #5, `docs/wallac-plate-reader-integration.md`,
  `docs/automation-integrations.md`

This document defines the Wallac bridge's controlled abort behavior, recovery
semantics, and incident/rollback sequence.

## Abort sources and latency

| Source | Path | Latency | Type |
|---|---|---|---|
| eLabFTW `Requested action = abort` | Bridge polls eLabFTW every ~5 s | 5-15 s | Non-real-time operator cancel intent |
| Dashboard "Request controlled abort" | Direct call to bridge runtime | <1 s | Lower-latency software abort |
| Physical emergency stop | Hardware button / Wallac console | Immediate | Emergency stop — **not** handled by the bridge |

**eLabFTW abort is not an emergency stop.** It is operator cancel intent that
the bridge processes when it next polls. The dashboard abort is faster because
it talks to the bridge runtime directly, but it is still a software abort — it
calls `POST /runs/{id}/abort` on the vm-agent, which is subject to the
instrument's 60-second minimum abort age.

## State machine: abort lifecycle

```
running → abort_requested → aborting → aborted
                                  ↘ failed (abort itself failed)
abort_requested → results_ready (run finished before abort took effect)
```

- **Abort before run starts** (accepted/queued/validating/ready → aborted):
  No physical work was done. The job goes directly to `aborted`.
- **Abort during run** (running → abort_requested → aborting → aborted):
  The execution loop detects the abort request, calls the vm-agent abort
  endpoint, and transitions to `aborted` on success or `failed` if the
  instrument did not respond.
- **Abort after completion** (terminal state): No-op. The abort request is
  logged but does not change the state.

## Recovery on restart

After restart, network interruption, service error, or operator abort, the
bridge classifies each persisted job state into one of four terminal states:

| Persisted state | has_results | Final state |
|---|---|---|
| `completed` | — | `completed` |
| `failed` | — | `failed` |
| `aborted` | — | `aborted` |
| `results_ready` / `results_uploaded` | yes | `completed` |
| `results_ready` / `results_uploaded` | no | `unknown_requires_operator_review` |
| `running` / `abort_requested` / `aborting` | — | `unknown_requires_operator_review` |
| `queued` / `validating` / `ready` | — | `unknown_requires_operator_review` |

**Never automatically repeat ambiguous physical work.** If the persisted state
is ambiguous (any active state), the bridge marks the job
`unknown_requires_operator_review` and does not re-execute. The operator must
inspect the instrument and results manually before creating a new signed job.

## Incident / rollback sequence

When a job fails, aborts, or enters an ambiguous state:

1. **Halt the run.** If the job is still running, call
   `POST /runs/{id}/abort` on the vm-agent. If the abort fails, log the error
   and mark the job `failed`.

2. **Restore a known-good state.** The Wallac Victor2 has no homing or
   voltage-restoration sequence — the carrier returns to its idle position
   when the measurement stops (or is aborted). No operator disassembly is
   required. If the instrument is in an error state, use
   `POST /admin/reconnect` to re-establish the COM link.

3. **Preserve local state.** The bridge writes the final state, error code,
   and operator hint to eLabFTW. If write-back fails (network error), the
   state is preserved locally and retried.

4. **Mark for operator review if ambiguous.** If the bridge cannot determine
   whether the run completed, it writes
   `unknown_requires_operator_review` to eLabFTW with a structured error
   (code, severity, human_message, operator_hint, retryable, details).

5. **Do not auto-retry.** The bridge never automatically re-executes a job
   that reached an ambiguous or failed state. The operator must create a new
   signed Automation Job to retry.

6. **Operator review.** The operator inspects the instrument, checks for
   partial results in the vm-agent's job database, and either:
   - Marks the job as `completed` if results exist, or
   - Creates a new signed Automation Job to re-run the assay.

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

- State machine: `bridge/lifecycle.py` — `LifecycleManager`
- Recovery: `bridge/lifecycle.py` — `RecoveryManager`
- Abort detection: `bridge/abort.py` — `AbortDetector` (eLabFTW polling),
  `DashboardAbortHandler` (direct)
- Tests: `tests/test_bridge_lifecycle.py`
