# Architecture: Direct-Submit Model

- **Status:** architecture decision
- **Date:** 2026-06-27
- **Supersedes:** eLabFTW-as-job-queue model (polling, claiming, metadata state machine)
- **Repos:** `Lambda-Biolab/wallac-victor2-api`, `antomicblitz/elabftw-lambdabiolab`

## Decision

eLabFTW is the **archive**, not the job queue, intent surface, or runtime
gatekeeper. The Run Builder submits jobs directly to the bridge via HTTP POST.
The bridge executes and writes results to eLabFTW as experiment records.

## Why

The previous architecture forced eLabFTW into roles it wasn't designed for:
job queue (polling), state machine (metadata-encoded lifecycle), runtime
gatekeeper (signing before execution), and credential proxy (passphrase
handling). Every piece of operator friction came from these mismatched roles.

The new model uses eLabFTW for what it's good at — durable storage, identity,
audit trail — and moves job submission, state management, and execution control
to the bridge, where they belong.

## Architecture

```
Quick run:
  User → Run Builder → POST /jobs (bridge) → vm-agent → Instrument
                                        ↓
                                   eLabFTW (experiment + results)

Validated workflow:
  User → Run Builder → eLabFTW (create + sign specs)
       → Run Builder → POST /jobs (bridge) → vm-agent → Instrument
                                        ↓
                                   eLabFTW (experiment + results)
```

## Role assignments

| Role | Old (eLabFTW-as-queue) | New (direct-submit) |
|---|---|---|
| Intent surface | eLabFTW (create Automation Job resource) | Run Builder (one-click) |
| Authorization | eLabFTW signing (cryptographic gate) | Run Builder auth (user session) |
| Job queue | eLabFTW polling (bridge polls every N seconds) | Bridge direct submit (HTTP POST) |
| State machine | eLabFTW metadata field (16-state select) | Bridge in-memory / event log |
| Spec storage | eLabFTW attachments | eLabFTW attachments (unchanged) |
| Result archive | eLabFTW Automation Job | eLabFTW experiment (new) |
| Audit trail | eLabFTW signatures + changelog | eLabFTW experiment + bridge event log |
| Abort | eLabFTW metadata field change (polled) | Bridge HTTP endpoint (real-time) |

## What stays the same

- **Canonical JSON schemas** (`wallac.method.v1`, `wallac.layout.v1`,
  `wallac.analysis.v1`, `wallac.job.v1`) — unchanged.
- **Deterministic serialization + SHA-256 hashing** — unchanged.
- **eLabFTW resource categories** (Method, Plate Layout, Analysis Plan) —
  still used for validated workflows. Operators create and sign these in
  eLabFTW (or via the Run Builder, which calls the eLabFTW API).
- **vm-agent REST API** — unchanged.
- **Analysis pipeline** — unchanged.
- **Result spool** — unchanged (still used for write-back resilience).
- **Dashboard** — still served by the bridge, but no longer needs to poll
  eLabFTW for state. The bridge knows its own state.

## What changes

### Removed: eLabFTW as job queue

- **No more polling.** The bridge does not poll eLabFTW for Automation Jobs.
- **No more `JobIntake`** module. Jobs arrive via HTTP POST.
- **No more `AbortDetector`** polling eLabFTW. Aborts arrive via HTTP.
- **No more `Automation state` metadata field** with 16 states. The bridge
  tracks state internally.
- **No more `Requested action` metadata field.** Submit and abort are HTTP
  calls.
- **No more `Claimed by` / `Claimed at` metadata fields.** The bridge
  doesn't claim — it receives.

### New: Bridge HTTP submit endpoint

The bridge exposes `POST /jobs` accepting a job spec:

```json
{
  "title": "OD600 run — E. coli growth",
  "execution_mode": "existing_protocol",
  "protocol_name": "Absorbance @ 600 (1.0s)",
  "elabftw_experiment_id": 42,
  "method_ref": {"object_id": 10, "hash": "...", "attachment_id": 5001},
  "layout_ref": {"source": "reusable", "object_id": 11, "hash": "...", "attachment_id": 5002},
  "analysis_ref": {"object_id": 12, "hash": "...", "attachment_id": 5003}
}
```

The bridge returns a job ID immediately and executes asynchronously:

```json
{"job_id": "job-abc123", "status": "accepted"}
```

### New: Bridge HTTP abort endpoint

`POST /jobs/{job_id}/abort` — real-time abort, no polling latency.

### New: Bridge HTTP status endpoint

`GET /jobs/{job_id}` — returns current state, progress, and results.

### Changed: Run Builder

The Run Builder calls the bridge directly instead of creating an Automation
Job in eLabFTW and waiting for the bridge to poll.

For quick runs: one click → `POST /jobs` → done.
For validated workflows: create + sign specs in eLabFTW → `POST /jobs` with
references → done.

### Changed: eLabFTW Automation Job category

The "Wallac Victor2 Automation Job" resource category is **deprecated** as a
queue object. It may still exist for historical/audit purposes, but the bridge
no longer polls it.

The metadata schema is simplified — the 16-state lifecycle, `Requested action`,
`Claimed by/at`, and other bridge-managed fields are removed. What remains:
execution mode, protocol name, method/layout/analysis references, expected
outputs, and result summary.

### Changed: Results written to experiments

Results are written to a new eLabFTW **experiment** (from the Wallac Victor2
Assay template), not to an Automation Job resource. The experiment is the
scientific record. The bridge creates it automatically after execution.

## Two execution paths

### Quick run (existing_protocol)

1. User opens Run Builder
2. Picks a protocol from the dropdown (fetched from vm-agent)
3. Optionally draws a plate layout (or skips — measure all wells)
4. Clicks "Run"
5. Run Builder calls `POST /jobs` on the bridge
6. Bridge executes, writes results to a new eLabFTW experiment
7. Run Builder shows results inline + link to eLabFTW experiment

No eLabFTW resources created before execution. No signing. One click.

### Validated workflow (generated_protocol)

1. User opens Run Builder
2. Walks through the 5-step wizard (Method, Layout, Analysis, Job, Finalize)
3. Run Builder creates draft resources in eLabFTW and finalizes canonical JSON
4. User signs resources in eLabFTW (audit trail — not a runtime gate)
5. Run Builder calls `POST /jobs` on the bridge with references to signed specs
6. Bridge validates signed specs, executes, writes results to eLabFTW experiment

Signing is for provenance, not for gating. The bridge receives the job directly.

## Migration

The old polling modules (`intake.py`, `abort.py`, `lifecycle.py`, `models.py`,
`writeback.py`) are deprecated. They remain in the repo for reference but are
not used by the new direct-submit path. New modules:

- `bridge/jobs.py` — job manager (receive, queue, execute, status)
- `bridge/execution.py` — updated to work without eLabFTW polling
- `bridge/writeback.py` — updated to write to experiments, not Automation Jobs

The `main.py` bridge daemon no longer runs a poll loop. Instead, it starts an
HTTP server (FastAPI) that accepts job submissions.
