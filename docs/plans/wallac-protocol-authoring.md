# Plan: Wallac Victor2 eLabFTW Protocol Authoring

Date: 2026-06-26
Target repo: `Lambda-Biolab/wallac-victor2-api`
Plan branch: `plan/wallac-protocol-authoring`
Status: approved planning artifact; implementation not started here

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
7. Require explicit submit/request execution after signature.

Signature validity is necessary but not sufficient. The bridge must also check signer identity against a static configured authorized-signer allowlist for v1. Dynamic eLabFTW team/group lookup is future work.

The Wallac bridge/designer service identity may create/update drafts, attach canonical JSON, update metadata summaries/hashes, and write back results, but it must not count as an authorized human/operator signer for executable approval. The same authorized human may sign Method, Layout, Analysis Plan, and Automation Job in v1; two-person approval is future work.

Bypasses are allowed only for tests/dev drafts and never for real MDB writes or instrument execution.

## UX and service boundaries

The main user-facing workflow is one guided Plate Reader Run Builder wizard. Users should not manually stitch resources together.

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

Use a simple oldest-first queue for valid submitted Automation Jobs.

- Bridge claims one Wallac job at a time.
- No priorities in v1.
- Queued jobs are not guaranteed executable until live preflight passes.
- If a queued job becomes invalid while waiting, it fails closed with operator hint.

Automation Job is one execution attempt:

- validate-only may repeat on the same job;
- once MDB generation or physical execution may have occurred, rerun requires a new Automation Job;
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

Mostly reuse existing Automation Job states:

- `draft`;
- `requested`;
- `accepted`;
- `queued`;
- `validating`;
- `ready`;
- `running`;
- `abort_requested`;
- `aborting`;
- `aborted`;
- `failed`;
- `results_ready`;
- `results_uploaded`;
- `completed`;
- `unknown_requires_operator_review`.

Avoid adding `writeback_pending` as top-level state in v1. Use `results_ready` plus metadata such as:

- `execution_mode`;
- `validation_status`;
- `generated_protocol_status`;
- `generated_assay_prot_id`;
- `writeback_spool_status`.

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

- Add schema modules for Method/Layout/Analysis/Job.
- Add deterministic serializer.
- Add exact-byte hash helpers.
- Add golden fixtures and mismatch tests.
- No eLabFTW writes, MDB writes, or instrument calls.

Acceptance:

- canonical fixtures stable;
- hash mismatch/attachment mismatch tests fail closed;
- schema unsupported/future version tests fail closed.

### Stage 2: eLabFTW setup/docs contract

- Prepare supporting eLabFTW repo plan/patch for category/template migration.
- Document object model, signatures, canonical attachments, designer links, and stale-doc correction.
- Keep runtime implementation in Wallac repo.

Acceptance:

- dry-run migration shows safe create/patch/skip behavior;
- docs distinguish generated authoring from legacy existing-protocol execution.

### Stage 3: authenticated designer/Run Builder drafts

- Add Linux-side web backend/framework if needed.
- Implement authenticated draft APIs.
- Backend finalizes canonical JSON and attaches draft files to eLabFTW.
- Draft objects mutable; signed objects immutable.
- No execution.

Acceptance:

- browser never receives eLabFTW key or vm-agent token;
- draft mutation allowed;
- signed mutation rejected or routed to clone/version.

### Stage 4: validation-only bridge path

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

### Stage 5: vm-agent generated-protocol support behind disabled flag

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

### Stage 6: bridge generated execution path

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

### Stage 7: hardware e2e acceptance and production enablement

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

- exact MDB columns required for each template copy/patch;
- exact selected-column fingerprint fields per mode;
- exact PlateMap binary encoding and bit order, verified by round-trip tests;
- exact result-table fields for OEM OD vs vm-agent-derived OD;
- exact live-result polling cadence and dashboard rendering shape;
- exact web framework choice for Linux-side designer backend;
- exact static signer allowlist config shape;
- exact spool retention defaults;
- exact generated protocol group name and installation checklist;
- exact per-mode hardware acceptance sequence.

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
