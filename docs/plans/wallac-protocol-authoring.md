# Plan: Wallac Victor2 eLabFTW Protocol Authoring

Date: 2026-06-26
Target repo: `Lambda-Biolab/wallac-victor2-api`
Plan branch: `plan/wallac-protocol-authoring`
Status: **Architecture updated: direct-submit model (bridge HTTP API, no eLabFTW polling). eLabFTW is the archive and audit trail, not the job queue, intent surface, or runtime gatekeeper. The Run Builder submits jobs directly to the bridge via `POST /jobs`. The bridge executes and writes results to eLabFTW as experiment records. Old polling modules (intake.py, abort.py, lifecycle.py, models.py, writeback.py) deprecated — kept for reference but not used by the direct-submit path. Stages 1–6 implemented and tested. `existing_protocol` execution path validated end-to-end on live hardware. `generated_protocol` path validated end-to-end. Run Builder UI implemented with drag-select plate layout editor. `make validate` green (297 tests). Remaining: OEM OD comparison, cleanup dry-run, abort during generated run, dedicated eLabFTW service key, 7 unmatched plasmid-primer links, 6 Phase 2 decisions.**

## Purpose

Implement constrained Wallac Victor2 protocol authoring from eLabFTW while keeping eLabFTW as the canonical source of truth and treating Wallac MDB protocols as generated execution artifacts/cache.

The new flow must let an authenticated operator use an external Wallac Run Builder to author or select a Method, Plate Layout, Analysis Plan, and Assay, sign the frozen execution bundle in eLabFTW, have the bridge validate exact canonical JSON bytes and signatures, generate one guarded MDB protocol for the job, execute it on the Wallac, analyze results, and write durable artifacts back to eLabFTW.

## Source-of-truth model

- eLabFTW is the source of truth for Methods, Plate Layouts, Analysis Plans, Automation Jobs, Assays, signatures, provenance, and results.
- Wallac MDB protocols are generated execution artifacts/cache.
- Generated MDB protocols are never canonical records.
- The bridge executes only the exact canonical JSON bytes whose SHA-256 hash matches signed eLabFTW metadata.
- Any missing signature, invalid signature, unauthorized signer, attachment mismatch, hash mismatch, stale lifecycle state, unsupported schema, or post-signature mutation fails closed before MDB generation or execution.

## v1 scope

### Supported measurement modes

v1 supports constrained single-read/single-label endpoint authoring for:

- photometry / absorbance;
- simple fluorometry;
- luminescence.

Out of scope for v1:

- TRF / DELFIA;
- LANCE;
- fluorescence polarization;
- advanced time-gating;
- G-factor;
- dispenser workflows;
- kinetic loops;
- scans;
- multi-label sequences;
- dual-wavelength correction;
- calibration curves;
- complex temperature programs;
- inventory consumption or volume tracking;
- arbitrary plate geometries beyond the configured 96-well plate type.

### Per-mode constraints

Photometry:

- one installed photometry filter per run;
- no arbitrary wavelength typing in canonical execution fields;
- UI aliases such as `OD600` may be displayed, but canonical execution uses physical Wallac filter identity such as `P610`;
- Method stores resolved filter ID/name and explicit read/integration settings;
- OD provenance must be explicit: OEM/Wallac-reported OD is preferred when available; vm-agent-computed OD is diagnostic/provisional unless validated against OEM output.

Simple fluorometry:

- one excitation filter;
- one emission filter;
- one read/integration setting;
- no scans, ratios, dual labels, TRF timing, polarization, or correction factors.

Luminescence:

- simple endpoint luminescence only;
- no excitation/emission filters;
- one integration/counting setting;
- no dispenser-triggered reads, kinetic loops, delayed reads, or multi-step sequences.

Temperature:

- omitted from executable Method schema;
- any vm-agent temperature/status value is telemetry-only;
- no target temperature, tolerance, timeout, or MDB temperature programming in v1.

Plate format:

- v1 supports only the configured installed 96-well plate type;
- canonical well names are `A1` through `H12`;
- canonical well ordering is row-major: `A1, A2, ..., A12, B1, ..., H12`;
- vm-agent must round-trip test MDB `PlateMap` encode/decode before generated-protocol authoring is enabled.

## eLabFTW object model

Use instrument-specific resource/experiment names:

- `Wallac Victor2 Method`;
- `Wallac Victor2 Plate Layout`;
- `Wallac Victor2 Analysis Plan`;
- `Wallac Victor2 Automation Job`;
- `Wallac Victor2 Assay`.

Rename existing generic templates in place where present:

- `Automation Job` -> `Wallac Victor2 Automation Job`;
- `Plate Reader Assay` -> `Wallac Victor2 Assay`.

Do not delete/recreate categories that may already contain records. Migrate idempotently, patching template bodies/metadata in place and creating only missing categories.

### Object responsibilities

Method:

- reusable acquisition settings;
- mode, installed filter/filter-pair or luminescence settings, plate type, integration/exposure/counting settings, and executable instrument-resolved IDs/units;
- does not own the measured-well set.

Plate Layout:

- well/sample map and measured/skipped/excluded well intent;
- reusable layouts are first-class signed resources;
- one-off layouts are signed canonical `layout.json` attachments on the Automation Job, optionally copied or linked to the Assay.

Analysis Plan:

- reusable analysis rules;
- blank subtraction, replicate aggregation, normalization, thresholds/pass-fail, exclusions, and output requirements.

Automation Job:

- one execution attempt;
- final frozen execution bundle;
- owns state transitions, signed input bundle, generated `AssayProtID`, validation report, event log, raw/analyzed artifacts, errors, rollback hints, spool status, and result manifest.

Assay:

- human scientific narrative;
- purpose, sample/control summary, selected analyzed results, conclusions, and links back to the Automation Job/artifacts;
- default: Run Builder creates one new Assay per submitted run;
- advanced: operator may attach a new Automation Job to an existing Assay when intentionally grouping related reads.

## Canonical JSON contracts

Canonical specs live as attached JSON files:

- `method.json`;
- `layout.json`;
- `analysis.json`;
- `job.json`.

eLabFTW metadata mirrors only summary/search fields and signed hash/attachment identity.

### Deterministic serialization

The backend, not the browser, is the canonicalization authority.

- UTF-8 bytes;
- sorted keys;
- no insignificant whitespace;
- explicit `schema_name` and `schema_version`;
- SHA-256 computed over exact attached bytes;
- bridge downloads the exact signed attachment ID, hashes bytes, compares to signed metadata, and only then parses JSON.

Supported v1 schema names:

- `wallac.method.v1`;
- `wallac.layout.v1`;
- `wallac.analysis.v1`;
- `wallac.job.v1`.

The bridge accepts only explicitly supported schema versions. Unknown/future versions fail closed. Schema migrations create new draft objects/attachments and new signatures; they never silently convert signed JSON in place.

### Signature binding metadata

Each executable eLabFTW object must have signed metadata binding both attachment identity and content hash:

- Method: `method_json_attachment_id`, `method_hash`;
- reusable Layout: `layout_json_attachment_id`, `layout_hash`;
- Analysis Plan: `analysis_json_attachment_id`, `analysis_hash`;
- Automation Job: `job_json_attachment_id`, `job_hash`, referenced object IDs/hashes, and one-off layout hash/attachment ID when applicable.

Replacing an attachment after signing fails closed unless a new signature is created.

## Lifecycle and versioning

Use a shared lifecycle model for executable objects:

- `draft`;
- `signed/active`;
- `superseded`;
- `rejected`;
- `archived`;
- `revoked` where needed.

Signed Method/Layout/Analysis objects are immutable. Editing a signed object creates a new draft clone/version with lineage fields:

- object kind;
- version;
- lifecycle status;
- parent object ID;
- supersedes object ID;
- content hash;
- canonical JSON attachment ID.

Automation Jobs bind to specific signed object versions by ID and hash. They must never resolve `latest active` at execution time.

Execution eligibility for referenced reusable objects:

- `signed/active`: allowed;
- `draft`: never allowed;
- `rejected`, `archived`, `revoked`: never allowed;
- `superseded`: not selectable for new jobs and not executable in v1 unless a later policy explicitly allows pending historical jobs.

The bridge revalidates eligibility immediately before MDB generation and again before run start if generation/execution are separate. After physical execution starts, later object lifecycle changes do not automatically stop or roll back the run; only explicit abort paths can stop it.

## Signing and authorization

Required signatures before generated-protocol execution:

- Method;
- reusable Plate Layout if used;
- Analysis Plan;
- Automation Job.

For one-off layouts, `layout.json` is attached to and covered by the Automation Job signature rather than a separate Layout resource signature.

Signing order:

1. Create or select draft Method, Layout, and Analysis Plan.
2. Finalize canonical JSON attachments and hashes.
3. Require signatures on Method, reusable Layout if used, and Analysis Plan.
4. Create Automation Job referencing exact signed object IDs and hashes.
5. Include one-off layout hash/attachment directly in `job.json` when applicable.
6. Require the Automation Job signature last.

> **In the direct-submit model, signing is for audit trail and provenance, not a
> runtime gate.** The Run Builder submits the job directly to the bridge via
> `POST /jobs` with references to signed specs. The bridge validates signed
> specs before execution, but signing no longer blocks submission. The operator
> signs in eLabFTW before or after creating the job — signing documents
> intent and authorship for the record.

Signature validity is necessary but not sufficient. The bridge must also check signer identity against a static configured authorized-signer allowlist for v1. Dynamic eLabFTW team/group lookup is future work.

The Wallac bridge/designer service identity may create/update drafts, attach canonical JSON, update metadata summaries/hashes, and write back results, but it must not count as an authorized human/operator signer for executable approval. The same authorized human may sign Method, Layout, Analysis Plan, and Automation Job in v1; two-person approval is future work.

Bypasses are allowed only for tests/dev drafts and never for real MDB writes or instrument execution.

## UX and service boundaries

The main user-facing workflow is one guided Plate Reader Run Builder wizard. Users should not manually stitch resources together. **In the direct-submit model, the Run Builder is the intent surface** — not eLabFTW. The operator sets up and submits runs entirely within the Run Builder UI. eLabFTW is the durable archive: it stores signed specs before execution and receives results afterward.

Rich Method/Layout/Analysis/Run Builder UIs live outside eLabFTW in this Wallac service repo. eLabFTW stores links that open designers in a new tab. No iframe/embed is required or assumed.

Authentication and secrets:

- designer and Run Builder require authenticated operator access;
- browser never receives the eLabFTW API key;
- browser never receives the vm-agent bearer token;
- browser talks to the Linux-side Wallac service only;
- Wallac service talks to eLabFTW with its service identity;
- Wallac service talks to vm-agent using configured URL/token;
- vm-agent remains private hardware/MDB adapter behind the bridge.

Browser validation is advisory only. The backend repeats all executable validation. The backend finalizes canonical JSON and computes hashes.

Allow adding a small Linux-side web framework for designer/Run Builder APIs, with pinned dependencies and tests. Keep Windows vm-agent dependency-light and Python 3.8 / Windows 7 compatible.

## Automation Job execution modes

Keep two distinct execution modes:

- `generated_protocol`: new strict v1 authoring path requiring signed `job.json`, `method.json`, `layout.json`, and `analysis.json`;
- `existing_protocol`: legacy/advanced compatibility path for running pre-existing Wallac/OEM protocols by signed Automation Job reference to existing protocol name or `AssayProtID`.

The main Run Builder creates `generated_protocol` jobs only. Existing-protocol execution remains advanced/operator/debug compatibility and must not claim Method/Layout/Analysis lineage unless those signed objects are actually present.

Manual metadata-only jobs cannot trigger generated MDB authoring. Generated authoring requires signed canonical `job.json`.

## Plate layout semantics

Use hybrid Plate Layout storage:

- reusable layouts are signed `Wallac Victor2 Plate Layout` resources;
- one-off layouts default to signed canonical `layout.json` attachments on the Automation Job;
- one-off layouts may be copied or linked to the Assay for readability/audit.

Distinguish unmeasured vs excluded wells:

- unmeasured/skipped wells are not included in MDB `PlateMap`; the instrument skips them;
- excluded wells are measured and preserved in raw outputs but excluded from analysis calculations.

Artifacts should include all 96 wells in analyzed per-well outputs:

- skipped wells: `measurement_status = skipped`, empty raw values;
- measured wells: raw values;
- excluded wells: raw values plus `analysis_excluded = true` and optional reason;
- summaries use only measured, non-excluded wells.

Plate Layout may include well-level `sample_name` / `sample_label` and optional linked eLabFTW item/resource IDs. It must not mutate inventory, decrement volumes, or mark samples consumed in v1.

## Generated MDB protocol model

Create one generated MDB protocol per Automation Job. Store generated `AssayProtID` back on the Automation Job.

Generated protocol identity:

- job-scoped and immutable;
- name format: `ELAB-Job-<automation_job_id>-<short_hash>`;
- execute by stored numeric `AssayProtID`, not by name;
- never automatically reuse generated protocols or IDs.

ID namespace:

- reserve a high generated `AssayProtID` range starting at or above `2000000`;
- collision-check `AssayProtocol.AssayProtID` before insert;
- never reuse IDs automatically in v1, even after cleanup.

Mutation scope:

- generator only inserts/copies new generated protocol rows needed for execution;
- never modifies installed filters, filter-slide positions, plate types, sample types, protocol groups, factory protocols, or user GUI protocols;
- new protocol links to existing reference IDs.

Generated rows:

- copy one known-safe operator-installed template for selected mode;
- patch only validated fields such as IDs, `ProtName`, `MeasSequence`, `PlateMap`, `PlateTypeID`, filters, integration/exposure/counting settings;
- create/copy exactly one mode-specific label/settings row in `Photometry`, `Fluorometry`, or `Luminometry`;
- leave unknown/OEM-specific columns at template defaults.

Template governance:

- safe template protocols are operator-installed prerequisites created/verified in OEM GUI;
- bridge/vm-agent never edits templates;
- each template has expected `AssayProtID`, mode, expected shape, and fingerprint;
- vm-agent fails closed if template is missing, drifted, or mode/shape mismatch is detected.

Protocol group:

- require a pre-existing dedicated MDB `ProtocolGroup`, e.g. `eLabFTW Generated`;
- vm-agent refuses generated protocol creation if the group is missing;
- do not create/modify protocol groups during job execution.

## MDB write safety

Add explicit vm-agent generated-protocol endpoints separate from normal run execution, for example:

- `POST /generated-protocols/validate`;
- `POST /generated-protocols`;
- `DELETE /generated-protocols` for cleanup dry-run/confirm.

Safety requirements:

- generated authoring disabled by default in production;
- real MDB writes require explicit feature flag such as `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`;
- per-mode readiness flags/gates decide whether photometry, fluorometry, or luminescence can execute;
- vm-agent writes only when instrument is idle and not in error;
- single writer lock covers MDB backup, validation, transaction/write, post-write verification, and handoff to execution;
- multiple draft/design operations can run concurrently, but no two jobs generate or start against the same MDB/instrument concurrently;
- create timestamped MDB backup before every write attempt and record path/checksum;
- use MDB transactions where the driver supports them;
- pre-commit failures roll back transaction;
- post-commit verification failures become operator-review incidents, not auto-repair;
- no automatic MDB backup restore in v1.

Post-write verification must include:

- database-level checks: generated `AssayProtocol`, generated `ProtName`, non-factory flag, correct `MeasSequence`, correct label row, `PlateMap`, filter/plate references;
- API-level checks: `GET /protocols/{AssayProtID}` resolves exactly one generated protocol with expected name/group/version.

## Cleanup

Generated MDB cleanup is operator/admin-only maintenance, never automatic job rollback.

Cleanup requirements:

- endpoint defaults to dry-run;
- requires explicit confirm;
- deletes only generated `ELAB-Job-*` protocols;
- deletes only terminal jobs older than configured N days;
- refuses deletion unless generated `AssayProtID` links back to a terminal Automation Job record;
- never touches factory protocols or user GUI protocols;
- records cleanup as a maintenance event.

## Queueing and run semantics

Jobs arrive via direct HTTP `POST /jobs` to the bridge, not via eLabFTW polling.

- The bridge accepts jobs immediately and returns a job ID.
- The bridge executes one job at a time (no parallelism for v1).
- No priorities in v1.
- Jobs are not guaranteed executable until live preflight passes.
- If a job becomes invalid while waiting, it fails closed with operator hint.

A job is one execution attempt:

- validate-only may repeat on the same job;
- once MDB generation or physical execution may have occurred, rerun requires a new job;
- rerun jobs use lineage fields pointing to the prior job and generate a new `ELAB-Job-*` MDB protocol.

If MDB generation succeeds but validation/execution/abort/result upload later fails, do not automatically delete or reuse the generated MDB protocol. Preserve generated `AssayProtID`, hashes, backup path, validation report, and event log for audit until explicit cleanup.

## Analysis

Analysis runs in the Linux-side Wallac bridge/service, not inside Windows vm-agent.

vm-agent responsibilities:

- COM/OEM interaction;
- MDB reads/writes;
- starting runs;
- abort;
- retrieving raw result rows.

Bridge/service responsibilities:

- apply signed `analysis.json` to raw results;
- produce raw/analyzed artifacts;
- upload artifacts and manifests to eLabFTW;
- write Assay summary.

Use `primary_value` abstraction:

- photometry stores OD plus raw counts/signals; `primary_value` prefers OEM OD when available;
- fluorometry/luminescence store raw intensity/counts as `primary_value`;
- all analysis operations work on `primary_value`.

Fixed v1 analysis pipeline order:

1. load raw per-well values;
2. mark skipped/unmeasured wells;
3. apply analysis exclusions;
4. compute blank from non-excluded blank wells;
5. subtract blank where configured;
6. compute normalization factor from control wells/groups;
7. apply normalization where configured;
8. aggregate replicate groups: mean, SD, CV, N;
9. apply thresholds/pass-fail rules;
10. emit raw, analyzed per-well, replicate summary, and analysis summary artifacts.

Output artifacts:

- raw, unmodified `raw_results.json` and/or `raw_results.csv`;
- `analyzed_wells.csv`;
- `replicate_summary.csv`;
- `replicate_summary.json`;
- `analysis_summary.json`.

Analysis provenance must include:

- `analysis_plan_object_id`;
- `analysis_hash`;
- analysis schema version;
- analysis engine/package version or git SHA;
- input raw artifact hash;
- timestamp.

If physical run succeeds and raw results are retrieved but analysis fails, upload/spool raw results and mark job `unknown_requires_operator_review`; do not fabricate analyzed outputs or mark completed.

## Result completeness and live preview

After a generated-protocol run finishes, verify raw result completeness before analysis/completion:

- every expected measured well has a raw result or explicit instrument/status reason for absence;
- skipped/unmeasured wells do not unexpectedly appear as measured unless flagged;
- duplicate rows, missing wells, unknown wells, or mode-mismatched values move job to `unknown_requires_operator_review`;
- raw results are still uploaded/spooled for audit;
- analysis runs only after completeness passes.

Add best-effort live result monitoring to dashboard/Run Builder:

- show run state, instrument state, progress, expected measured wells, live raw values, missing/pending wells, skipped wells, and excluded wells;
- label live data as preliminary until terminal completeness checks, signed analysis, artifact upload, and final write-back finish;
- final scientific results come only from terminal raw artifact plus completeness gate plus signed analysis pipeline.

## Write-back and local spool

Automation Job is authoritative home for execution artifacts. Assay receives readable summary and links.

If instrument run succeeds and raw results are retrieved but eLabFTW write-back fails:

- do not rerun plate;
- persist local pending result package;
- retry write-back;
- job not fully completed until eLabFTW write-back succeeds;
- operators see measurement-succeeded/write-back-pending or failed status.

Local spool implementation:

- simple filesystem spool, not a new database;
- configured directory such as `WALLAC_RESULT_SPOOL_DIR`;
- one immutable subdirectory per Automation Job/run attempt;
- atomic temp-write/fsync/rename;
- includes manifest, artifacts, checksums, job ID, generated `AssayProtID`, retry state;
- no eLabFTW API keys, vm-agent tokens, session tokens, or bearer tokens in spool;
- spool may contain scientific raw/analyzed results;
- permissions restricted to bridge service account and operators/admins;
- finalized entries retained only through configured short grace period or moved/deleted after successful write-back;
- pending/failed entries remain until operator resolution.

## State model and events

In the direct-submit model, the bridge manages state internally (not in eLabFTW metadata). The simplified state set is:

- `accepted` — job received and queued;
- `running` — execution in progress;
- `completed` — execution succeeded, results written;
- `failed` — execution failed before instrument work;
- `aborted` — execution halted by operator;
- `unknown_requires_operator_review` — ambiguous state after restart or partial failure.

The bridge tracks additional metadata internally (validation status, generated protocol status, write-back status) without encoding them as job-level states.

Use append-only event log entries for generated-authoring boundaries:

- draft finalized / canonical hash written;
- signatures verified and signers authorized;
- lifecycle eligibility checked;
- live capability/MDB preflight;
- MDB backup created;
- generated protocol dry-run/validation;
- MDB rows written;
- post-write verification;
- run started with generated `AssayProtID`;
- raw results retrieved;
- completeness checked;
- analysis success/failure;
- artifacts uploaded or spooled;
- Assay summary updated;
- cleanup maintenance events.

## Error taxonomy

Define stable machine-readable generated-authoring errors with retryability, whether physical work may have occurred, human message, and operator hint.

Initial codes:

- `canonical_hash_mismatch`;
- `canonical_attachment_mismatch`;
- `schema_unsupported`;
- `signature_missing`;
- `signature_invalid`;
- `signer_unauthorized`;
- `referenced_object_not_active`;
- `capability_unavailable`;
- `mode_not_enabled`;
- `template_missing_or_drifted`;
- `mdb_id_collision`;
- `mdb_backup_failed`;
- `mdb_write_failed`;
- `post_write_verification_failed`;
- `result_incomplete`;
- `analysis_failed`;
- `writeback_spooled`;
- `operator_review_required`.

## API documentation

Add explicit service API contracts before or alongside implementation.

Document/OpenAPI-style coverage for:

- designer draft APIs for Method/Layout/Analysis/Job;
- canonical JSON finalization APIs;
- validation-only endpoint/report shape;
- generated-protocol vm-agent endpoints;
- cleanup dry-run/confirm endpoint;
- result spool/admin endpoints;
- error codes and operator-hint fields;
- live result/status dashboard stream.

## Supporting eLabFTW repo changes

Primary implementation belongs in this repo. Supporting changes in `antomicblitz/elabftw-lambdabiolab` should be planned separately and limited to:

- idempotent Wallac category/template migration;
- renaming generic templates in place;
- operator docs;
- stale automation docs update to state that Wallac consumes signed canonical eLabFTW resources for generated authoring;
- links from eLabFTW records to external Wallac designers/Run Builder.

Do not add Wallac runtime services to the core eLabFTW compose as part of v1.

## Implementation sequence

### Stage 1: schemas, canonicalization, tests only

**Status: ✅ DONE (merged)**

- Add schema modules for Method/Layout/Analysis/Job.
- Add deterministic serializer.
- Add exact-byte hash helpers.
- Add golden fixtures and mismatch tests.
- No eLabFTW writes, MDB writes, or instrument calls.

Acceptance:

- canonical fixtures stable;
- hash mismatch/attachment mismatch tests fail closed;
- schema unsupported/future version tests fail closed.

Implementation: `bridge/canonical.py`, `bridge/schemas.py`, 8 golden fixtures, 58 tests.

### Stage 2: eLabFTW setup/docs contract

**Status: ✅ DONE (merged)**

- Prepare supporting eLabFTW repo plan/patch for category/template migration.
- Document object model, signatures, canonical attachments, designer links, and stale-doc correction.
- Keep runtime implementation in Wallac repo.

Acceptance:

- dry-run migration shows safe create/patch/skip behavior;
- docs distinguish generated authoring from legacy existing-protocol execution.

Implementation: `docs/elabftw-object-model.md`, `tools/elab-seed/seed_wallac.py` (213 tests), 5 categories created/renamed on live eLabFTW.

### Stage 3: authenticated designer/Run Builder drafts

**Status: ✅ DONE (merged)**

- Add Linux-side web backend/framework if needed.
- Implement authenticated draft APIs.
- Backend finalizes canonical JSON and attaches draft files to eLabFTW.
- Draft objects mutable; signed objects immutable.
- No execution.

Acceptance:

- browser never receives eLabFTW key or vm-agent token;
- draft mutation allowed;
- signed mutation rejected or routed to clone/version.

Implementation: `bridge/designer.py` (DesignerService), `bridge/designer_app.py` (FastAPI app with CRUD + finalize + clone, served at `/run-builder`), `bridge/run_builder.html` (single-page wizard UI with 5-step flow: Method → Plate Layout → Analysis → Job → Review & Finalize; drag-select plate grid; eLabFTW URL config + "Open in eLabFTW" links; next-steps panel after finalization). 31 tests.

### Stage 4: validation-only bridge path

**Status: ✅ DONE (merged)**

- Implement signed canonical attachment verification.
- Implement signer allowlist.
- Implement lifecycle eligibility checks.
- Implement live vm-agent health/capability checks.
- Implement Method/Layout/Analysis/Job consistency checks.
- Implement MDB generation dry-run plan without writes.
- Write validation report to Automation Job.

Acceptance:

- valid bundle passes validation-only;
- missing/invalid/unauthorized signatures fail closed;
- stale lifecycle or hash mismatch fails closed;
- validate-only never mutates MDB or starts run.

Implementation: `bridge/validation.py` (ValidationService with signed attachment verification, signer allowlist, lifecycle eligibility, vm-agent capability checks). 18 tests.

### Stage 5: vm-agent generated-protocol support behind disabled flag

**Status: ✅ DONE (merged + deployed to live VM)**

- Add generated-protocol validate/create/delete endpoints.
- Add operator-installed template config/fingerprints.
- Add generated ID allocation and collision checks.
- Add MDB backup and checksums.
- Add single-writer lock.
- Add transaction/write and post-write verification.
- Add cleanup dry-run/confirm.
- Keep disabled by default in production.

Acceptance:

- test MDB fixtures cover template copy/patch, collision, drift, backup failure, rollback, post-write verification, and cleanup filters;
- no generated writes unless feature flag and mode gate are enabled.

Implementation:
- `bridge/generated_protocols.py` (GeneratedProtocolManager with ID allocation, collision detection, backup, single-writer lock, post-write verification, cleanup). 23 tests.
- `vm-agent/agent.py`: 9 new MDB endpoints (`GET /mdb/groups`, `GET /mdb/protocols/{id}`, `GET /mdb/protocols?name=`, `GET /mdb/max-protocol-id`, `POST /mdb/protocols`, `DELETE /mdb/protocols/{id}`, `POST /mdb/backup`, `POST /mdb/query`, `POST /mdb/groups`). Single-writer lock, feature flag `WALLAC_ENABLE_PROTOCOL_AUTHORING`.
- `bridge/remote_mdb_client.py` (RemoteMdbClient implementing MdbClient Protocol via HTTP to vm-agent). 28 tests.
- `bridge/factory.py` (create_orchestrator wiring all components from BridgeConfig).
- **Deployed to live VM** (`C:\install\agent.py` on `win7-wallac`). All 9 endpoints verified against live MDB. `eLabFTW Generated` protocol group created (GroupID=10001). Authoring flag enabled via `C:\install\run_agent.bat` (scheduled task `wallac-agent`).

### Stage 6: bridge generated execution path

**Status: ✅ DONE (merged + validated on live hardware for existing_protocol mode)**

- Add `generated_protocol` execution mode.
- Add oldest-first queue and claim behavior.
- Generate protocol, store `AssayProtID`, execute by ID.
- Add live result preview stream.
- Add result completeness gate.
- Add analysis execution and artifact production.
- Add local filesystem spool for write-back outage.
- Add Assay summary write-back.

Acceptance:

- mocked vm-agent/eLabFTW tests cover success, validation failures, generation failures, run failures, incomplete results, analysis failure, write-back spool/retry, abort/recovery.

Implementation:
- `bridge/execution.py` (ExecutionOrchestrator: validation → generation → run → completeness → analysis → spool → write-back → Assay summary). `check_result_completeness()`, `_write_assay_summary()`, `_build_assay_body()`.
- `bridge/analysis.py` (AnalysisPipeline: 10-step analysis — blank subtraction, normalization, replicate aggregation, thresholds, artifact export).
- `bridge/spool.py` (ResultSpool: atomic writes, manifest, retry, secret scan).
- `bridge/vm_agent_client.py` (VmAgentClient: HTTP client for all vm-agent endpoints).
- `bridge/intake.py` (JobIntake: polling, signature verification, claiming) — **deprecated** in direct-submit model; kept for reference.
- `bridge/abort.py` (AbortDetector: eLabFTW abort polling) — **deprecated** in direct-submit model; aborts arrive via `POST /jobs/{id}/abort`.
- `bridge/dashboard.py` (DashboardServer: SSE live progress, plate view, abort).
- `bridge/writeback.py` (WriteBackManager: throttled progress, terminal result packages) — **deprecated** in direct-submit model; write-back is synchronous via the execution pipeline.
- `bridge/lifecycle.py` (LifecycleManager, RecoveryManager) — **deprecated** in direct-submit model; state is managed in-memory by the bridge.
- `bridge/models.py` (state machine models) — **deprecated** in direct-submit model; simplified states are tracked internally.
- `main.py` (BridgeDaemon: poll loop, abort thread, spool drain, dashboard server) — updated for direct-submit model: HTTP server (FastAPI) accepts job submissions instead of polling eLabFTW.
- `deploy/wallac-bridge.service` (systemd unit), `deploy/bridge.env.example`.
- 262 tests total (290 with RemoteMdbClient tests).

**Live validation (existing_protocol mode, pre-direct-submit):**
- Bridge daemon running on `lambdabiolab-computer`, polling eLabFTW every 5s (old polling model).
- Two jobs completed successfully (#326, #327): 96-well reads at 600nm, real OD data, `raw_results.json` uploaded, Assay experiments created and linked, event logs posted.
- The direct-submit architecture (adopted 2026-06-27) replaces the polling model. The bridge now exposes an HTTP API and receives jobs via `POST /jobs` instead of polling eLabFTW.
- Signature verification working (minisign, signer identity, post-signature integrity).
- Bugs found and fixed during live testing:
  1. `list_uploads` didn't include archived uploads (state=2) — signature archives invisible.
  2. `extract_signed_request_fields` didn't handle list-format `data.json` from eLabFTW 5.5.14.
  3. `DEFAULT_ELABFTW_CATEGORY` was items_types ID (9), not items_categories ID (21).
  4. `link_experiment_to_item` sent item_id in body instead of URL path (500 error).

### Stage 7: hardware e2e acceptance and production enablement

**Status: ⏳ PARTIALLY DONE — `existing_protocol` path validated end-to-end on live hardware (no plate loaded; results were air readings). `generated_protocol` path not yet tested with real plate.**

- Run real eLabFTW -> bridge -> vm-agent -> Wallac -> raw results -> analysis -> artifacts -> Assay summary flow.
- Validate generated MDB protocol in OEM/Wallac context.
- Validate photometry first if that mode is ready before others.
- Compare photometry OD values against OEM export before relying on vm-agent-derived OD.
- Enable modes independently only after their hardware e2e passes.

Acceptance:

- real hardware run succeeds end-to-end;
- signed bundle hashes verified;
- generated `ELAB-Job-*` protocol created with backup and post-write verification;
- run executes by numeric `AssayProtID`;
- result completeness matches signed layout;
- raw and analyzed artifacts/checksums/manifests written to eLabFTW;
- Assay summary links to Automation Job;
- cleanup dry-run lists only eligible generated protocols;
- production feature flag remains off until operator approval.

**Completed:**
- ✅ `existing_protocol` path validated end-to-end (Jobs #326, #327): signature verification, claiming, 96-well measurement, results upload, Assay creation + linking, event log.
- ✅ `generated_protocol` path validated end-to-end (Job #337): signed Method/Layout/Analysis/Job bundle, canonical JSON hash verification, MDB protocol generation (2000002), 96-well photometry run, result completeness check, analysis (blank subtraction, replicate aggregation, pass/fail), 5 artifacts uploaded to eLabFTW, job marked `completed`.
- ✅ Designer app deployed on `lambdabiolab-computer` (FastAPI + uvicorn on port 8422).
- ✅ Signed bundle created via designer API: Method #334 (photometry, P610, 0.1s), Layout #335 (96-well, all measured), Analysis #336 (no transforms), Job #337.

**Bugs found and fixed during `generated_protocol` live testing (8 commits):**

1. **`op_mdb_insert_protocol` used DAO AddNew/Update** — fails with comtypes on Jet. Fixed: use SQL INSERT (same pattern as ProtocolGroup creation).
2. **Generated protocol only had basic columns** — missing PlateMap, MeasurementMode, etc. Fixed: `generate_protocol()` now copies the full template row.
3. **Direct `bytes` assignment to PlateMap field fails** — comtypes can't marshal `bytes` to COM VARIANT. Fixed: use `array.array('B', ...)` with `AppendChunk()`.
4. **`NormalizationInfo` is also a binary OLE Object field** — was in SQL INSERT, causing "Data type conversion error". Fixed: added to `_BINARY_COLS` set.
5. **`NormalizationInfo` is NULL in template** — `list(None)` raises TypeError. Fixed: skip None binary fields.
6. **DAO AppendChunk fails on NULL OLE Object fields** — Jet doesn't allow appending binary data to a field initialized as NULL by SQL INSERT. Fixed: clone template via `INSERT INTO ... SELECT` (copies binary fields in one SQL step), then `UPDATE` specific fields.
7. **vm-agent returns `{"well": "A1"}` but analysis expected `{"well_name": "A1"}`** — KeyError in `_load_raw`. Fixed: `_well_key()` helper checks both keys.
8. **vm-agent zero-pads well names (A01, A02)** but layout specs don't (A1, A2) — all 96 wells reported missing. Fixed: `_normalize_well_name()` strips leading zeros.

**Remaining (non-blocking for v1 go-live):**
- ❌ OEM OD comparison not done (Test 5).
- ❌ Cleanup dry-run not tested on live MDB (Test 6).
- ❌ Abort during generated run not tested (Test 8).
- ❌ Bridge daemon not installed as systemd service (running as nohup process).
- ❌ Designer app not deployed as persistent service (running as nohup).
- ❌ Dedicated eLabFTW service API key not created (using admin key).

**Test plan:** `docs/stage7-hardware-e2e-test-plan.md` (8 test sequences).

## Test strategy

Every stage requires automated tests before merge.

Required test groups:

- canonicalization golden bytes and hash mismatch;
- signature valid/missing/invalid/modified/unauthorized;
- draft mutation vs signed immutability;
- validation-only with mocked vm-agent capabilities and MDB plans;
- MDB fixtures for template copy, ID collision, backup, transaction rollback, post-write verification, cleanup dry-run filtering;
- analysis fixtures for blank subtraction, normalization, replicate stats, exclusions, skipped wells, thresholds, failure cases;
- result completeness fixtures;
- filesystem spool crash-safety, retry, no-secret scan;
- live preview preliminary vs final result state;
- final real hardware e2e gate.

## Rollback and incident policy

- Do not automatically repeat ambiguous physical work.
- Do not automatically delete generated protocols after failure.
- Do not automatically restore MDB backups.
- Preserve generated protocol, backup path/checksum, validation report, raw results if any, event log, and operator hint.
- Route uncertain post-commit or post-execution states to `unknown_requires_operator_review`.
- Reruns require new Automation Jobs.
- Cleanup is explicit operator/admin maintenance only.

## Open implementation discovery tasks

These are not product decisions; resolve during implementation and testing:

- ~~exact MDB columns required for each template copy/patch~~ — resolved: `AssayProtID`, `ProtName`, `ProtNumber`, `ProtVersion`, `FactoryPreset`, `ProtGroup` (see `_ASSAY_PROTOCOL_COLUMNS` in `agent.py`); full row returned by `GET /mdb/protocols/{id}` includes all columns.
- ~~exact selected-column fingerprint fields per mode~~ — resolved: `ProtName`, `FactoryPreset`, `ProtGroup` (see `TemplateFingerprint` in `generated_protocols.py`).
- ~~exact PlateMap binary encoding and bit order, verified by round-trip tests~~ — resolved: PlateMap is a byte array in `AssayProtocol.PlateMap`, 384 bytes for 96-well (4 bytes per well). Verified via `GET /mdb/protocols/2000001` on live instrument.
- ~~exact result-table fields for OEM OD vs vm-agent-derived OD~~ — resolved: vm-agent returns `{well, od, counts, meas_a, meas_b, ...}`; OD is OEM-reported (preferred), counts are raw.
- ~~exact live-result polling cadence and dashboard rendering shape~~ — resolved: dashboard SSE at ~1Hz, plate view with per-well values, progress percent, state.
- ~~exact web framework choice for Linux-side designer backend~~ — resolved: FastAPI (see `bridge/designer_app.py`).
- ~~exact static signer allowlist config shape~~ — resolved: `SignerAllowlist.from_env()` reads `WALLAC_AUTHORIZED_SIGNERS` env var (comma-separated emails).
- ~~exact spool retention defaults~~ — resolved: `DEFAULT_GRACE_PERIOD = 86400` (24h), `DEFAULT_MAX_RETRIES = 10`.
- ~~exact generated protocol group name and installation checklist~~ — resolved: `eLabFTW Generated` (GroupID=10001), created via `POST /mdb/groups` endpoint. Installed on live VM.
- ~~exact per-mode hardware acceptance sequence~~ — resolved: photometry first (Test 4 in `docs/stage7-hardware-e2e-test-plan.md`), then fluorometry, then luminescence.

## Non-goals

- Full Wallac protocol authoring.
- Advanced modes: TRF/DELFIA, LANCE, FP, advanced time-gating, G-factor.
- Dispensers, kinetics, scans, multi-label workflows.
- Temperature control/programming.
- Inventory mutation or sample volume consumption.
- Dynamic eLabFTW team/group signer authorization.
- Two-person approval requirement.
- Automatic MDB backup restore.
- Automatic generated-protocol cleanup after each run.
- Reusing an Automation Job for reruns.
- Browser-side execution authority or browser-held secrets.

## Deployment status (as of 2026-06-27)

### Live infrastructure

| Component | Location | Status |
|-----------|----------|--------|
| eLabFTW | `antonios-beast` (Tailscale 100.119.135.27:3148) | Running, v5.5.14 |
| Bridge daemon (HTTP API) | `lambdabiolab-computer` (Tailscale 100.81.236.54) | Running as `systemd` service (`wallac-bridge.service`, enabled for boot). Direct-submit model: accepts jobs via `POST /jobs` instead of polling eLabFTW. |
| vm-agent | `win7-wallac` VM (libvirt NAT 192.168.122.203:8420) | Running via `C:\install\run_agent.bat` (sets `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`). Updated 2026-06-27 with all fixes (dedup ResultType 0, protocol cache refresh, INSERT INTO SELECT cloning). |
| Designer app | `lambdabiolab-computer` (port 8422) | Running as `systemd` service (`wallac-designer.service`, enabled for boot) |
| Instrument | Victor2 1420 | Connected, idle, working |

### Configuration

- Bridge env: `/etc/wallac-bridge/bridge.env` on `lambdabiolab-computer` (includes `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`, `WALLAC_ENABLE_PHOTOMETRY=true`, `WALLAC_PHOTOMETRY_TEMPLATE_ID=2000001`, `WALLAC_PHOTOMETRY_TEMPLATE_NAME="Absorbance @ 600 (0.1s)"`, `WALLAC_AUTHORIZED_SIGNERS=antonio@lambconsulting.bio`, `WALLAC_DESIGNER_TOKEN=`). API key: dedicated service user "Wallac Bridge" (userid=2, non-sysadmin), key id=5. In the direct-submit model, the bridge no longer needs the eLabFTW API key for polling — only for write-back (creating experiments, uploading results).
- vm-agent: `C:\install\agent.py` on `win7-wallac`, started by `C:\install\run_agent.bat` (sets `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`)
- eLabFTW signing key: created for user 1 (Antonio Lamb), passphrase `wallac2024`
- `eLabFTW Generated` protocol group: GroupID=10001 in MDB
- Spool dir: `/var/lib/wallac-bridge/spool`
- Dashboard: `http://lambdabiolab-computer:8421`
- Designer app: `http://lambdabiolab-computer:8422`

### What's NOT yet deployed

(none — all components deployed and running)

### Stage 7 — COMPLETE (all 8 tests pass)

| Test | Status | Bugs found & fixed |
|------|--------|-------------------|
| Test 1: MDB endpoint connectivity | ✅ Pass | — |
| Test 2: MDB backup | ✅ Pass | — |
| Test 3: Generated protocol CRUD | ✅ Pass | DAO AddNew/Update fails with comtypes → SQL INSERT |
| Test 4: Full generated_protocol e2e | ✅ Pass (Job #337) | 8 bugs (binary PlateMap, NormalizationInfo, bytes→array.array, AppendChunk on NULL → INSERT INTO SELECT, well key mismatch, well name normalization) |
| Test 5: OEM OD comparison | ✅ Pass | `_dedup_wells` picked wrong ResultType → prefer ResultType 0 (primary). 96/96 wells match within ±0.001. |
| Test 6: Cleanup dry-run | ✅ Pass | `cleanup_terminal()` in-memory only → query MDB via `ALIKE 'ELAB-Job-%'`. Defense-in-depth prefix filter. |
| Test 7: Feature flag enforcement | ✅ Pass | — |
| Test 8: Abort during generated run | ✅ Pass (Job #351) | 3 bugs (stale protocol cache → refresh on miss; 425 "too early" hard failure → retry; aborting/aborted race condition → check response state) |

**Total bugs found and fixed during Stage 7 live testing: 13**

All fixes committed, pushed, and deployed. `make validate` fully green (lint, format, complexity gate, 297 tests).

### Remaining work for Stage 7

1. ~~**`generated_protocol` path on live hardware`**~~ — ✅ DONE (Job #337 completed successfully: protocol generated, 96-well run, results analyzed, 5 artifacts uploaded, job marked `completed`).
2. ~~**OEM OD comparison** (Test 5)~~ — ✅ DONE (2026-06-27: compared vm-agent results for assay_id=40 against OEM MlrMgr export. **Bug found and fixed:** `_dedup_wells` was picking the first-seen ResultType row per well, which could be either primary (ResultType 0) or secondary (ResultType 3) depending on DB row order. This caused 6 of 96 wells to mismatch the OEM export. Fix: prefer ResultType 0 (primary) in dedup. After fix: all 96 wells match within ±0.001, max_diff=0.000500. Fix committed but **vm-agent needs restart** with updated `agent.py` from vm-share.)
3. ~~**Cleanup dry-run** (Test 6)~~ — ✅ DONE (predicate verified 2026-06-27: `ALIKE "ELAB-Job-%"` matches exactly the 1 generated protocol 2000002; 0 factory presets in generated group 10001; 0 user-GUI protocols match the prefix). **Defect found and fixed:** `GeneratedProtocolManager.cleanup_terminal()` only iterated in-memory `self._generated` dict — empty after bridge restart. Fixed: both `cleanup_terminal()` and `delete_protocol()` now query the MDB via `MdbClient.query()` with `ALIKE 'ELAB-Job-%'`. Defense-in-depth: results filtered again by `GENERATED_NAME_PREFIX` before any delete. 7 new tests cover the restart scenario.
4. ~~**Abort during generated run** (Test 8)~~ — ✅ DONE (2026-06-27: Job #351 aborted successfully. Run started, abort sent after 67s (past the Victor2's 60s minimum), bridge detected abort within 10s, vm-agent aborted the run, terminal state written as `aborted`. **Three bugs found and fixed during testing:** (1) `_resolve_protocol` used stale cache, couldn't find newly-generated protocols — fixed: refresh on miss; (2) abort poller treated vm-agent 425 "too early" as a hard failure — fixed: retry on next cycle; (3) abort poller's `aborting` write raced with main thread's `aborted` terminal state write — fixed: only write `aborting` if the run is still running.)
5. ~~**Install systemd service** for bridge daemon~~ — ✅ DONE (2026-06-27: `wallac-bridge.service` installed at `/etc/systemd/system/`, `active (running)`, `enabled` for boot. SELinux `bin_t` fcontext applied to `.venv/bin/` via `semanage fcontext` + `restorecon`. Security hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/wallac-bridge`, `ProtectHome=read-only`).
6. ~~**Deploy designer app** as a persistent service~~ — ✅ DONE (2026-06-27: `wallac-designer.service` installed at `/etc/systemd/system/`, `active (running)`, `enabled` for boot. Same SELinux `bin_t` fcontext. Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, no WritePaths — designer is FS read-only, talks to eLabFTW over HTTP).

### Key files for a new agent

- `docs/plans/wallac-protocol-authoring.md` — this plan
- `docs/architecture-direct-submit.md` — architecture decision for direct-submit model
- `docs/stage7-hardware-e2e-test-plan.md` — 8 test sequences for Stage 7
- `docs/api-reference.md` — complete API docs (vm-agent, designer, bridge HTTP API, error codes)
- `main.py` — bridge daemon entry point (HTTP server via FastAPI; no longer polls eLabFTW)
- `bridge/factory.py` — component wiring (`create_orchestrator()`, reads template config from env vars)
- `bridge/execution.py` — ExecutionOrchestrator (full pipeline: validation → generation → run → completeness → analysis → spool → write-back → Assay summary). Includes `_well_key()` and `_normalize_well_name()` for vm-agent↔layout well name normalization.
- `bridge/analysis.py` — AnalysisPipeline (10-step analysis — blank subtraction, normalization, replicate aggregation, thresholds, artifact export). Uses `_well_key()` from execution.py for raw well matching.
- `bridge/generated_protocols.py` — GeneratedProtocolManager (MDB protocol lifecycle). `generate_protocol()` clones template via INSERT INTO SELECT, overrides fields via UPDATE.
- `bridge/remote_mdb_client.py` — RemoteMdbClient (HTTP → vm-agent MDB endpoints)
- `vm-agent/agent.py` — vm-agent with MDB endpoints (deployed to `C:\install\agent.py`). `op_mdb_insert_protocol()` clones template row via SQL INSERT INTO SELECT (handles binary PlateMap/NormalizationInfo fields that can't be set via DAO AppendChunk on NULL fields).
- `deploy/wallac-bridge.service` — systemd unit (installed 2026-06-27, enabled for boot)
- `deploy/wallac-designer.service` — designer systemd unit (installed 2026-06-27, enabled for boot)
- `deploy/bridge.env.example` — env file template
- `tools/compare_od.py` — OEM OD comparison script for Test 5 (auto-detects well/OD columns in MlrMgr CSV/TXT export, compares against bridge raw_results.json)

### eLabFTW API gotchas (learned during live testing)

- `?cat=` parameter filters by `items_categories.id`, NOT `items_types.id`. Default category is 21 (not 9).
- Signature archives are stored as uploads with `state=2` (archived). Must query `?state=2` to find them.
- `data.json` in signature archive is a JSON array (API response format), not a dict.
- Signing requires a sig key pair: `POST /users/{id}/sig_keys` with `{"passphrase": "..."}`.
- Sign an entity: `PATCH /{entity_type}/{id}` with `{"action": "sign", "passphrase": "...", "meaning": 10}` (meaning is an integer: 10=Approval, 20=Authorship, etc.).
- Link experiment to item: `POST /experiments/{id}/items_links/{item_id}` with empty JSON body (item_id in URL path, not body).
- `patch_metadata` must read current metadata, merge new fields, and write back the full metadata JSON string.
- API key creation via DB insert: the key format is `<api_keys.id>-<secret>`. The bcrypt hash must be of the **secret part only**, not the full key. PHP's `password_verify()` is called on the secret after splitting by `-`. Use `$2y$` prefix (not `$2b$`) for PHP compatibility.
- Service user creation: `POST /users` with `{"firstname", "lastname", "email", "team", "usergroup"}`. User is created with `validated=1` and added to `users2teams`. Set `is_sysadmin=0` via DB update for least privilege.

### Remaining work (post-Stage-7)

1. ~~**Dedicated eLabFTW API key for bridge**~~ — ✅ DONE (2026-06-27: created dedicated service user "Wallac Bridge" (userid=2, non-sysadmin, team admin in Default team). API key `5-ee534a...` provisioned via direct DB insert with bcrypt hash. Bridge and designer restarted with new key. Verified: bridge polling eLabFTW successfully, `last_used_at` updating. Old admin key (`4-l4mbd4...`) still valid for admin scripting but no longer used by bridge.)
2. **7 unmatched plasmid-to-primer links** — operator decision needed on correct primer pairs (in `antomicblitz/elabftw-lambdabiolab` repo).
3. **6 Phase 2 decisions** (in `antomicblitz/elabftw-lambdabiolab` AGENT_REQUESTS.md): off-host backup target, SMTP provider, domain/DNS, Benchling review set, alerting target, Hetzner sizing.
4. **Complexity refactoring** — all 6 pre-existing cognitive complexity violations fixed (op_mdb_insert_protocol 36→8, _load_canonical_specs 25→4, Handler::do_GET 18→5, Handler::_get_jobs 20→5, Handler::_get_simple 16→4, _grid_csv 17→6). `make validate` fully green.
