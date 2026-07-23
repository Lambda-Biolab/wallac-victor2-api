# Plan: Wallac Victor2 eLabFTW Protocol Authoring

Date: 2026-06-26
Target repo: `Lambda-Biolab/wallac-victor2-api`
Plan branch: `plan/wallac-protocol-authoring`
Status: **Direct-submit model — bridge HTTP API (`POST /jobs`), no eLabFTW polling. eLabFTW is the archive and audit trail, not the job queue. Stages 1–7 implemented and tested end-to-end on live hardware. `existing_protocol` and `generated_protocol` execution paths validated. Run Builder UI (drag-select plate layout) deployed as systemd service. Remaining: 7 unmatched plasmid-primer links, 6 Phase 2 decisions.**

## Purpose

Implement constrained Wallac Victor2 protocol authoring from eLabFTW while keeping eLabFTW as the canonical source of truth and treating Wallac MDB protocols as generated execution artifacts/cache.

The new flow must let an authenticated operator use an external Wallac Run Builder to author or select a Method, Plate Layout, Analysis Plan, and Assay, sign the frozen execution bundle in eLabFTW, have the bridge verify attachment bytes against caller-supplied reference hashes, generate one guarded MDB protocol for the job, execute it on the Wallac, analyze results, and write durable artifacts back to eLabFTW.

## Source-of-truth model

- eLabFTW is the source of truth for Methods, Plate Layouts, Analysis Plans, Automation Jobs, Assays, signatures, provenance, and results.
- Wallac MDB protocols are generated execution artifacts/cache.
- Generated MDB protocols are never canonical records.
- The bridge accepts jobs via authenticated HTTP requests (bridge bearer token). It downloads the eLabFTW attachment referenced by the job request, computes its SHA-256 hash, and compares the result to the hash supplied in the request — a byte-level integrity check against caller-supplied reference hashes.
- Missing attachment, hash mismatch, or unsupported schema fails closed before MDB generation or execution. eLabFTW-native signing is provenance/audit convention, not a bridge-enforced execution gate.

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

- one execution attempt — records intent and provenance;
- final frozen execution bundle and signed input refs (attachment IDs and hashes).

Assay:

- human scientific narrative;
- purpose, sample/control summary, selected analyzed results, and conclusions;
- bridge records Automation Job metadata (job ID, execution summary) in the experiment HTML body without creating eLabFTW item links;
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
- bridge downloads the attachment by the caller-supplied ID, hashes bytes, compares to the caller-supplied hash, and only then parses JSON.

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

In the eLabFTW authoring workflow, execution eligibility for referenced reusable objects follows:

- `signed/active`: allowed;
- `draft`: never allowed;
- `rejected`, `archived`, `revoked`: never allowed;
- `superseded`: not selectable for new jobs.

These lifecycle checks are enforced by the Run Builder at submission time, not by the bridge at execution time.

## Signing and authorization

Required signatures before generated-protocol execution (eLabFTW authoring convention — not validated by the bridge at runtime):

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
> `POST /jobs` with reference hashes from signed eLabFTW objects. The bridge
> compares downloaded attachment bytes to the caller-supplied hashes before
> execution. The operator signs in eLabFTW before or after creating the job —
> signing documents intent and authorship for the record.

The bridge validates integrity by downloading the canonical JSON attachment from the signed eLabFTW object and verifying its SHA-256 hash against the caller-supplied reference hash. Signer identity is managed entirely within eLabFTW's native signing — the bridge does not maintain an independent signer allowlist. Dynamic eLabFTW team/group lookup is future work.

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

- `generated_protocol`: new strict v1 authoring path requiring signed `job.json`, `method.json`, `layout.json`, and `analysis.json` (eLabFTW authoring convention; the bridge checks only hash integrity and schema version);
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

A temporary clone protocol is created for the run, started, and cleaned up.
The clone's AssayProtID exists only in bridge runtime state (JobManager), not
persisted on the Automation Job.

Generated protocol identity:

- temporary clone per run, not a persistent generated protocol;
- name format: ``ELAB-Run-{new_id}`` where ``new_id`` is derived from ``int(time.time()) % 100000 + 2001000``;
- the clone is cleaned up after execution (deleted in a ``finally`` block via ``_cleanup_cloned_protocol()``);
- the factory template is never modified;
- the original protocol ID (not the clone) is used for name resolution; the clone ID is used only for starting the run.

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
- the vm-agent's ``op_mdb_insert_protocol`` clones the template row by
  ``AssayProtID`` (``_template_id``); if the template ID is missing or
  the row does not exist, the insert fails closed.
- no template fingerprint or drift validation is performed beyond existence
  — the template must be present and match the expected mode at runtime.

Protocol group:

- require a pre-existing dedicated MDB `ProtocolGroup`, e.g. `eLabFTW Generated`;
- vm-agent refuses generated protocol creation if the group is missing;
- do not create/modify protocol groups during job execution.

## MDB write safety

Add explicit vm-agent generated-protocol endpoints separate from normal run execution:

- `POST /mdb/protocols` — create protocol;
- `DELETE /mdb/protocols/{id}` — cleanup leftover protocols.

Safety requirements:

- generated authoring disabled by default in production;
- real MDB writes require explicit feature flag ``WALLAC_ENABLE_PROTOCOL_AUTHORING=true`` on the vm-agent;
- vm-agent writes only when instrument is idle and not in error;
- single writer lock covers protocol creation, validation, post-write verification, and handoff to execution;
- multiple draft/design operations can run concurrently, but no two jobs generate or start against the same MDB/instrument concurrently;
- use MDB transactions where the driver supports them;
- pre-commit failures roll back transaction;
- post-commit verification failures become operator-review incidents, not auto-repair.

Post-write verification must include:

- database-level checks: generated `AssayProtocol`, generated `ProtName`, non-factory flag, correct `MeasSequence`, correct label row, `PlateMap`, filter/plate references;
- API-level checks: `GET /mdb/protocols/{AssayProtID}` resolves exactly one generated protocol with expected name/group/version.

## Cleanup

The executor automatically cleans up cloned protocols after each run (``_cleanup_cloned_protocol()`` in ``bridge/executor.py``, called unconditionally from ``finally``). No manual cleanup cycle is required.

The vm-agent's ``DELETE /mdb/protocols/{id}`` endpoint remains available for ad-hoc cleanup of any leftover protocols.

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
- rerun jobs use lineage fields pointing to the prior job and generate a new temporary clone.

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
- produce raw results JSON and, if analysis configured, analyzed CSV;
- upload artifacts to eLabFTW experiment as attachments;
- write experiment HTML body (results summary, Automation Job metadata).

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
10. emit raw results JSON and, if analysis configured, analyzed CSV with well-level results.

Output artifacts:

- raw, unmodified `raw_results.json` (guaranteed — always uploaded);
- `analyzed_wells.csv` (optional — present only if analysis ran);
- experiment HTML summary written to the eLabFTW experiment body.

If physical run succeeds and raw results are retrieved but analysis fails, the executor logs an in-memory `analysis_failed` event, uploads raw JSON (omits analyzed CSV), and continues to completion. The normal results HTML is still patched into the experiment body — it simply lacks analyzed-well data.

## Result normalization and live preview

After raw results are retrieved, the executor normalises/filters the well set
(marks unmeasured wells, deduplicates) and proceeds directly to analysis and
write-back. There is no formal completeness gate that can halt the pipeline.

> **Future:** A formal result-completeness gate (verify every expected measured
> well has a result before analysis) is not implemented in v1.

``BridgeExecutor._poll_run()`` (``bridge/executor.py``) accumulates live
well values as they are measured and stores them in ``job.live_wells``.
These are exposed via ``GET /jobs/{job_id}`` in the ``live_wells`` field
and are consumed by the Run Builder's real-time heatmap:

- shows run state, progress, expected measured wells, live raw values, and missing/pending wells;
- labels live data as preliminary until terminal analysis, artifact upload, and final write-back finish;
- final scientific results come only from terminal raw artifact plus signed analysis pipeline.

## Write-back

The eLabFTW experiment and its attachments are the authoritative home for
execution artifacts. The Automation Job records intent and provenance; it is
not the artifact store. Write-back is synchronous and part of the execution
pipeline (``BridgeExecutor`` in ``bridge/executor.py``).

If instrument run succeeds and raw results are retrieved but eLabFTW write-back
fails:

- do not rerun the plate;
- the job is marked ``failed`` with an event log entry — there is no local
  spool or retry queue;
- the operator inspects the vm-agent's run history for raw results and
  re-submits if needed.

## State model and events

In the direct-submit model, the bridge manages state internally (not in eLabFTW metadata). The simplified state set is:

- `accepted` — job received and queued;
- `running` — execution in progress;
- `completed` — execution succeeded, results written to eLabFTW experiment;
- `failed` — execution failed before instrument work;
- `aborted` — execution halted by operator;
- `unknown_requires_operator_review` — ambiguous state after restart or partial failure.

The bridge tracks additional metadata internally (validation status, generated protocol status, write-back status) without encoding them as job-level states.

Use append-only event log entries for generated-authoring boundaries:

- draft finalized / canonical hash written;
- specs downloaded and hash-verified against ref metadata;
- live capability/MDB preflight;
- generated protocol dry-run/validation;
- MDB rows written;
- post-write verification;
- run started with generated `AssayProtID`;
- raw results retrieved;
- analysis success/failure;
- raw and analyzed artifacts uploaded to eLabFTW;
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
- `operator_review_required`.

## API documentation

Add explicit service API contracts before or alongside implementation.

Document/OpenAPI-style coverage for:

- designer draft APIs for Method/Layout/Analysis/Job;
- canonical JSON finalization APIs;
- generated-protocol vm-agent endpoints;
- error codes and operator-hint fields;
- live result/status stream.

## Supporting eLabFTW repo changes

Primary implementation belongs in this repo. Supporting changes in `antomicblitz/elabftw-lambdabiolab` should be planned separately and limited to:

- idempotent Wallac category/template migration;
- renaming generic templates in place;
- operator docs;
- stale automation docs update to state that Wallac consumes signed canonical eLabFTW resources for generated authoring;
- links from eLabFTW records to external Wallac designers/Run Builder.

Do not add Wallac runtime services to the core eLabFTW compose as part of v1.

### Stages 1–7: Implementation sequence

All stages implemented, tested, and deployed to production. Key outcomes by stage:

| Stage | Scope | Status |
|-------|-------|--------|
| 1 | Canonical JSON schemas, deterministic serialization, hash helpers (`bridge/canonical.py`, `bridge/schemas.py`, 58 tests) | ✅ Merged |
| 2 | eLabFTW category/template migration, object-model docs (`docs/elabftw-object-model.md`, `tools/elab-seed/seed_wallac.py`) | ✅ Merged |
| 3 | Authenticated designer/Run Builder API + SPA (`bridge/designer.py`, `bridge/designer_app.py`, `bridge/run_builder.html`, 31 tests) | ✅ Merged |
| 4 | Hash-verified canonical attachment download from signed eLabFTW objects, vm-agent capability checks — integrated into ``BridgeExecutor`` | ✅ Merged |
| 5 | vm-agent generated-protocol endpoints (create/delete) behind ``WALLAC_ENABLE_PROTOCOL_AUTHORING`` flag; single-writer lock; ``eLabFTW Generated`` protocol group (GroupID=10001) created on live MDB | ✅ Deployed |
| 6 | Bridge execution pipeline (``BridgeExecutor`` in ``bridge/executor.py``): validation → generation → run → raw result processing → analysis → write-back. ``AnalysisPipeline`` in ``bridge/analysis.py``. No local spool — write-back is synchronous. | ✅ Merged + validated |
| 7 | Hardware e2e: all 8 test sequences pass on live Victor2. 13 bugs found and fixed during live testing (see eLabFTW API gotchas below for key learnings). ``wallac-bridge.service`` and ``wallac-designer.service`` installed as systemd services. Dedicated eLabFTW service API key provisioned. | ✅ Complete |

**Stage 7 acceptance:**
- ✅ Full generated-protocol e2e (Job #337): signed bundle → MDB protocol (2000002) → 96-well photometry → analysis → raw results JSON uploaded, experiment body patched with HTML → job ``completed``.
- ✅ OEM OD comparison (Test 5): all 96 wells match within ±0.001 after ``_dedup_wells`` fix (prefer ResultType 0).
- ✅ Abort during generated run (Test 8, Job #351): ``POST /jobs/{id}/abort`` forwarded to vm-agent; run stopped after 67 s. Three bugs fixed: stale protocol cache refresh, 425 retry, aborting/aborted race condition.
- ✅ Systemd services installed with systemd sandboxing hardening (``NoNewPrivileges``, ``ProtectSystem=strict``).
- ✅ Dedicated eLabFTW service API key (user "Wallac Bridge", userid=2, non-sysadmin).

**Test plan:** ``docs/stage7-hardware-e2e-test-plan.md`` (8 test sequences).

## Test strategy

Every stage requires automated tests before merge.

Required test groups:

- canonicalization golden bytes and hash mismatch;
- hash-verified attachment download from signed eLabFTW objects;
- draft mutation vs signed immutability;
- validation-only with mocked vm-agent capabilities and MDB plans;
- MDB fixtures for template copy, ID collision, transaction rollback, and post-write verification;
- analysis fixtures for blank subtraction, normalization, replicate stats, exclusions, skipped wells, thresholds, failure cases;
- live preview preliminary vs final result state;
- final real hardware e2e gate.

## Rollback and incident policy

- Temporary ELAB-Run clones are best-effort automatically cleaned in ``finally``.
- Leftover clones from interrupted cleanup may be removed ad hoc via ``DELETE /mdb/protocols/{id}``.
- Do not automatically repeat ambiguous physical work.
- Do not automatically restore MDB backups (none are created).
- Preserve raw results if any and event log (operator hints are in-memory only).
- Reruns require new Automation Jobs.

## Open implementation discovery tasks

These are not product decisions; resolve during implementation and testing:

- ~~exact MDB columns required for each template copy/patch~~ — resolved: minimal insert columns are ``AssayProtID``, ``ProtName``, ``ProtNumber``, ``ProtVersion``, ``FactoryPreset``, ``ProtGroup``; the vm-agent clones the full template row via ``INSERT INTO ... SELECT``.
- ~~exact selected-column fingerprint fields per mode~~ — resolved: ``ProtName``, ``FactoryPreset``, ``ProtGroup``.
- ~~exact PlateMap binary encoding and bit order, verified by round-trip tests~~ — resolved: PlateMap is a byte array in `AssayProtocol.PlateMap`, 384 bytes for 96-well (4 bytes per well). Verified via `GET /mdb/protocols/2000001` on live instrument.
- ~~exact result-table fields for OEM OD vs vm-agent-derived OD~~ — resolved: vm-agent returns `{well, od, counts, meas_a, meas_b, ...}`; OD is OEM-reported (preferred), counts are raw.
- ~~exact live-result polling cadence~~ — resolved: bridge polls vm-agent at ~1 Hz; live well values exposed via ``GET /jobs/{job_id}``.
- ~~exact web framework choice for Linux-side designer backend~~ — resolved: FastAPI (see `bridge/designer_app.py`).
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
- Reusing an Automation Job for reruns.
- Browser-side execution authority or browser-held secrets.

## Deployment status

### Live infrastructure

| Component | Location | Status |
|-----------|----------|--------|
| eLabFTW | `antonios-beast` (Tailscale 100.119.135.27:3148) | Running, v5.5.14 |
| Bridge daemon (HTTP API) | `lambdabiolab-computer` (Tailscale 100.81.236.54, port 8423) | ``systemd`` service (``wallac-bridge.service``, enabled). Accepts jobs via ``POST /jobs``. |
| vm-agent | ``win7-wallac`` VM (libvirt NAT 192.168.122.203:8420) | ``C:\install\agent.py`` started by ``C:\install\run_agent.bat`` (sets ``WALLAC_ENABLE_PROTOCOL_AUTHORING=true``) |
| Designer app | ``lambdabiolab-computer`` (port 8422) | ``systemd`` service (``wallac-designer.service``, enabled) |
| Instrument | Victor2 1420 | Connected, idle, working |

### Configuration

- **Bridge env** (``/etc/wallac-bridge/bridge.env``): ``WALLAC_ELABFTW_URL``, ``WALLAC_ELABFTW_API_KEY`` (dedicated service user, userid=2, non-sysadmin), ``WALLAC_VM_AGENT_URL``, ``WALLAC_VM_AGENT_TOKEN``, ``WALLAC_BRIDGE_TOKEN``, ``WALLAC_DESIGNER_TOKEN``, optional ``WALLAC_CORS_ORIGINS`` and ``WALLAC_REQUIRE_AUTH``. See ``deploy/bridge.env.example``.
- **vm-agent** (``C:\install\agent.py``): ``WALLAC_ENABLE_PROTOCOL_AUTHORING=true`` in ``run_agent.bat``; feature flag guards all generated-protocol endpoints.
- **eLabFTW signing key: [REDACTED]
- **``eLabFTW Generated`` protocol group:** GroupID=10001 in MDB
- **Designer app URL:** ``http://lambdabiolab-computer:8422``

### Stage 7 — complete

All 8 test sequences pass on live hardware. 13 bugs fixed during live testing; key learnings are captured in the eLabFTW API gotchas section below. ``make validate`` fully green.

### Key files for a new agent

- ``docs/plans/wallac-protocol-authoring.md`` — this plan
- ``docs/architecture-direct-submit.md`` — architecture decision for direct-submit model
- ``docs/stage7-hardware-e2e-test-plan.md`` — 8 test sequences for Stage 7
- ``docs/api-reference.md`` — vm-agent API reference
- ``bridge/bridge_app.py`` — FastAPI app, entry point for ``wallac-bridge.service``
- ``bridge/jobs.py`` — ``JobManager`` (in-memory job queue, duplicate detection, state tracking)
- ``bridge/executor.py`` — ``BridgeExecutor`` (execution pipeline: validation → protocol resolution → run → raw result processing → analysis → eLabFTW write-back)
- ``bridge/analysis.py`` — ``AnalysisPipeline`` (blank subtraction, normalization, replicate aggregation, thresholds, artifact export)
- ``bridge/elabftw.py`` — ``ElabftwClient`` (HTTP client for eLabFTW write-back)
- ``bridge/vm_agent_client.py`` — ``VmAgentClient`` (HTTP client for vm-agent endpoints)
- ``bridge/designer.py`` / ``bridge/designer_app.py`` — Run Builder backend/UI (``wallac-designer.service``)
- ``bridge/config.py`` — ``BridgeConfig`` (env-var driven, all secrets from runtime)
- ``vm-agent/agent.py`` — vm-agent with MDB endpoints (deployed to ``C:\install\agent.py``)
- ``deploy/wallac-bridge.service`` — bridge systemd unit
- ``deploy/wallac-designer.service`` — designer systemd unit
- ``deploy/bridge.env.example`` — env file template
- ``tools/compare_od.py`` — OEM OD comparison script for Test 5

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

1. ~~**Dedicated eLabFTW API key for bridge**~~ — ✅ DONE (dedicated service user "Wallac Bridge", userid=2, non-sysadmin, API key provisioned.)
2. **7 unmatched plasmid-to-primer links** — operator decision needed on correct primer pairs (in `antomicblitz/elabftw-lambdabiolab` repo).
3. **6 Phase 2 decisions** (in `antomicblitz/elabftw-lambdabiolab` AGENT_REQUESTS.md): off-host backup target, SMTP provider, domain/DNS, Benchling review set, alerting target, Hetzner sizing.
4. **Complexity refactoring** — all 6 pre-existing cognitive complexity violations fixed (op_mdb_insert_protocol 36→8, _load_canonical_specs 25→4, Handler::do_GET 18→5, Handler::_get_jobs 20→5, Handler::_get_simple 16→4, _grid_csv 17→6). `make validate` fully green.
