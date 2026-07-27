# Wallac Bridge — Existing-Protocol Writeback Repair

**Status:** approved; implementation in progress.
**Branch:** `fix/wallac-existing-protocol-writeback-repair` (wallac-victor2-api),
companion: `feat/wallac-run-current-experiment-id` (lab-copilot-gateway).

## Context

Experiment #125 (Strain growth validation in urine) hit three distinct
failures on the Wallac bridge at `http://100.81.236.54:8423` for a T=0 OD610
plate read:

1. **Protocol name spacing** — `Absorbance @ 610 (1.0 s)` (with space) was
   rejected by the bridge's protocol resolver while `Absorbance @ 610 (1.0s)`
   (no space) resolved to protocol id `2000008`.
2. **`wells_spec` body shape** — the bridge accepted
   `{"wells_spec": {"all": true}}` and `{"wells_spec": {"wells": "all"}}` at
   the HTTP boundary, but the vm-agent's
   `PATCH /mdb/protocols/{id}/wells` expects top-level
   `all` / `rows` / `wells` keys and rejected both shapes; dropping
   `wells_spec` entirely succeeded.
3. **eLabFTW writeback TLS** — after the run completed and 96 wells were
   measured, writeback to eLabFTW failed with
   `SSL: CERTIFICATE_VERIFY_FAILED` because the bridge reached
   `https://localhost:3148` (self-signed certificate) without a configured
   `WALLAC_ELABFTW_CA_BUNDLE`.

The user's prior `wallac-bridge-tls-trust.md` plan is approved; this plan
covers the gaps that remain (whitespace protocol matching, `wells_spec`
translation, append/upsert writeback for a caller-supplied experiment,
no-hardware writeback retry endpoint, gateway current-experiment passthrough).

## Decisions

- **Protocol-name resolution:** normalize whitespace (collapse runs of
  whitespace, strip whitespace before/after `(`, `)`, and before unit
  suffixes) before exact and substring matching. Preserve ID lookup and
  ambiguity handling.
- **`wells_spec`:** the bridge is the public contract owner. It validates
  shape, expands valid specs (`{all:true}` → 96 wells,
  `{rows:[...]}` → rows × 12 wells, `{wells:[...]}` → explicit list) to
  an explicit well list, and rejects malformed inputs at the HTTP
  boundary. Only `{wells:[...]}` (the canonical form) reaches the
  vm-agent.
- **TLS:** never disable verification. Deploy the canonical CA bundle
  for the dev self-signed eLabFTW cert and set
  `WALLAC_ELABFTW_CA_BUNDLE` to that path. The bridge code path that
  loads the bundle already exists.
- **Writeback to caller-supplied experiment:** append/upsert a delimited
  Wallac section rather than overwrite the experiment body. Only create
  a new experiment when the caller passes `elabftw_experiment_id == 0`.
  Use sentinel HTML comments
  (`<!-- WALLAC_RESULTS:<job_id>:START -->` / `:END -->`) so repeat
  writeback for the same job replaces the section in place.
- **Writeback retry:** add `POST /jobs/{job_id}/retry-writeback` for
  completed runs. Must never restart hardware acquisition. Must reuse
  stored result data; fail closed if results cannot be refetched.
- **Gateway behavior:** `wallac.run` sends the current experiment ID when
  one is in context (instead of forcing `0`). Standalone runs with no
  experiment context continue to create a new results experiment.

## Slice plan

### Slice 1 — Protocol-name whitespace normalization
- Code:
  - `vm-agent/agent.py::_resolve_protocol` — collapse internal whitespace
    on both the query and candidate names before exact/substring match.
  - `bridge/executor.py::_resolve_existing_protocol` — fall back to a
    whitespace-collapsed retry after a `404 protocol_not_found`, so
    caller-supplied names from older eLabFTW records resolve without
    changing canonical MDB names.
- Tests:
  - `tests/test_executor.py` — new
    `test_resolve_protocol_whitespace_normalization`.
  - `tests/test_jobs.py` — job with `protocol_name` containing an extra
    space before `s` resolves to the canonical protocol.
- Acceptance: pytest green; canonical IDs still resolve; ambiguous names
  still rejected.

### Slice 2 — `wells_spec` translation and validation
- Code:
  - `bridge/bridge_app.py::JobSubmitRequest.wells_spec` — tighten Pydantic
    schema with a typed model
    `WellsSpec(all=False, rows=None, wells=None)` and a model-level
    validator rejecting more than one of `all`/`rows`/`wells`.
  - `bridge/executor.py::_extract_wells_from_spec` — expand
    `all` / `rows` / `wells` to explicit well lists; reject non-list
    `wells` (e.g. `"all"`) at the boundary, not deep in the vm-agent
    call.
  - `vm-agent/agent.py::_wells_to_plate_map` — defensive: if a future
    caller passes `{"wells_spec": {...}}`, return a clear 400.
- Tests:
  - `tests/test_jobs.py` — boundary tests for each shape plus rejection
    of `{wells:"all"}` and `{all:true, rows:["A"]}`.
  - `tests/test_executor.py` — executor-level tests asserting the
    expanded well list for each shape.
- Acceptance: `{all:true}` produces 96 wells; `{rows:["A","B"]}`
  produces 24; `{wells:[...]}` round-trips; invalid shapes never reach
  the vm-agent.

### Slice 3 — TLS configuration (deployment, not code)
- Wallac host `/etc/wallac-bridge/bridge.env`: ensure
  `WALLAC_ELABFTW_CA_BUNDLE` is set to a readable PEM file containing the
  current dev CA.
- Restart `wallac-bridge.service` only after the env file is correct.
- No code change required unless the running process is stale relative to
  the bridge code that loads the bundle.

### Slice 4 — Append/upsert writeback for current experiment
- Code:
  - `bridge/elabftw.py::ElabftwClient.get_experiment_body(id)` — fetch
    current body.
  - `bridge/elabftw.py::ElabftwClient.patch_experiment(id, body)` —
    already exists; reuse.
  - `bridge/executor.py::_writeback` — when `exp_id > 0`, fetch body,
    upsert the delimited section for `job_id`, then patch. Preserve
    unrelated content. New-experiment path unchanged.
  - `_build_results_html` — emit
    `<!-- WALLAC_RESULTS:<job_id>:START -->...<!-- WALLAC_RESULTS:<job_id>:END -->`.
- Tests:
  - `tests/test_executor.py::TestWritebackPreservesExistingBody` — body
    with user content keeps that content after writeback.
  - `tests/test_executor.py::TestWritebackUpsertsSameJobSection` — second
    writeback for the same `job_id` replaces only that job's section.
- Acceptance: focused pytest green; no overwrite of unrelated content;
  repeat writeback idempotent for the same job.

### Slice 5 — No-hardware writeback retry endpoint
- Code:
  - `bridge/bridge_app.py::POST /jobs/{job_id}/retry-writeback` —
    authenticated, never restarts hardware.
  - `bridge/jobs.py::JobManager.retry_writeback(job_id)` — fetch stored
    results or re-fetch from the run id; call existing `_writeback`
    path.
  - Reject if job not completed, or if results cannot be located.
- Tests:
  - `tests/test_jobs.py` — completed job retries successfully; non-
    completed job is rejected with `409`.
- Acceptance: pytest green; no MDB-protocol mutation; no
  run-acquisition calls.

### Slice 6 — Gateway: current experiment ID passthrough
- Code:
  - `lab-copilot-gateway/src/lab_copilot_gateway/wallac.py::wallac.run` —
    pass `body.args.get("elabftw_experiment_id", current_exp_id or 0)`.
  - `wallac.py:898-913` comment block updated to reflect safe
    append/upsert semantics instead of "do not pass the current id".
- Tests:
  - `tests/test_wallac.py::test_run_passes_current_experiment_id` —
    asserts the bridge receives the context's experiment id.
  - `tests/test_wallac.py::test_run_creates_new_experiment_without_context`
    — asserts `0` is sent when no context exists.
- Acceptance: pytest green in `lab-copilot-gateway`.

### Slice 7 — Docs and TLS plan status alignment
- `wallac-victor2-api/docs/wallac-bridge-tls-trust.md` — keep canonical;
  do not duplicate content here.
- `eLabFTW-lambdabiolab/docs/plans/wallac-bridge-tls-trust.md` — keep
  the existing plan's slice tracker; do not move its content.
- `wallac-victor2-api/docs/api-reference.md` — add a section for
  `POST /jobs/{job_id}/retry-writeback` and document `wells_spec`
  accepted shapes.
- `AGENT_LEARNINGS.md` (eLabFTW ops) — append a short note linking the
  plan and documenting the canonical protocol name
  `Absorbance @ 610 (1.0s)` (no space).

## Risks

- Gateway change must ship **after** Slice 4. Until the bridge does
  append/upsert, sending the current experiment ID risks overwriting
  unrelated experiment content.
- Whitespace normalization must preserve ambiguity handling; do not let
  two distinct protocols collide after collapse.
- Wallac host certs may rotate; the CA bundle must be regenerated when
  the dev PKI rolls.
- The retry endpoint must never restart hardware acquisition. Strict
  guard required.

## Acceptance for the plan as a whole

- All new pytest tests pass in `wallac-victor2-api` and
  `lab-copilot-gateway`.
- `make check` (where applicable) passes in both repos.
- Manual end-to-end smoke (T=0 read against experiment #125-equivalent
  fixture) succeeds through writeback with the current experiment ID.
- TLS writeback succeeds with `WALLAC_ELABFTW_CA_BUNDLE` set.
- `wallac-bridge-tls-trust.md` plan status reflects the new work via
  cross-reference, not duplication.