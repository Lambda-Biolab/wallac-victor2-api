# eLabFTW object model for Wallac Victor2 protocol authoring

- **Status:** contract
- **Date:** 2026-06-26
- **Plan:** `docs/plans/wallac-protocol-authoring.md`
- **eLabFTW repo:** `antomicblitz/elabftw-lambdabiolab`

This document defines the eLabFTW object model for Wallac Victor2 protocol
authoring. It is the contract between the eLabFTW templates/categories managed
by the eLabFTW repo and the bridge/designer runtime implemented in this repo.

## Source-of-truth model

eLabFTW is the canonical source of truth for Methods, Plate Layouts, Analysis
Plans, Automation Jobs, Assays, signatures, provenance, and results. Wallac MDB
protocols are generated execution artifacts/cache — never canonical records.

The bridge accepts jobs via authenticated HTTP requests (bridge bearer token).
It downloads the eLabFTW attachment referenced by the job request, computes its
SHA-256 hash, and compares the result to the hash supplied in the request — a
byte-level integrity check against caller-supplied reference hashes. Missing
attachment, hash mismatch, or unsupported schema fails closed before MDB
generation or execution.

## Object types

Five eLabFTW object types participate in the v1 authoring flow:

| Object | eLabFTW type | Purpose |
|---|---|---|
| Wallac Victor2 Method | resource category (`items_types`) | Reusable acquisition settings |
| Wallac Victor2 Plate Layout | resource category | Well/sample map and measured/skipped/excluded well intent |
| Wallac Victor2 Analysis Plan | resource category | Reusable analysis rules |
| Wallac Victor2 Automation Job | resource category | One execution attempt — intent and provenance; frozen bundle and input refs |
| Wallac Victor2 Assay | experiment template (`experiments_template`) | Human scientific narrative |

Existing generic templates are renamed in place:

- `Automation Job` → `Wallac Victor2 Automation Job`
- `Plate Reader Assay` → `Wallac Victor2 Assay`

Migration is idempotent: the seed script patches titles/bodies/metadata in place
and creates only missing categories. It never deletes categories that may contain
records.

## Object responsibilities

### Method

Reusable acquisition settings. Does not own the measured-well set.

Fields: mode, installed filter/filter-pair or luminescence settings, plate type,
integration/exposure/counting settings, and executable instrument-resolved
IDs/units.

### Plate Layout

Well/sample map and measured/skipped/excluded well intent.

- Reusable layouts are first-class signed resources.
- One-off layouts are signed canonical `layout.json` attachments on the
  Automation Job, optionally copied or linked to the Assay.

Distinguishes unmeasured vs excluded wells:

- Unmeasured/skipped wells are not included in MDB `PlateMap`; the instrument
  skips them.
- Excluded wells are measured and preserved in raw outputs but excluded from
  analysis calculations.

### Analysis Plan

Reusable analysis rules: blank subtraction, replicate aggregation,
normalization, thresholds/pass-fail, exclusions, and output requirements.

### Automation Job

One execution attempt. Records intent and provenance: owns the frozen execution
bundle, signed input refs (attachment IDs and hashes), and binding metadata.
Runtime state, events, and clone protocol ID are tracked in the bridge's
in-memory JobManager. Execution results and analyzed artifacts are written to
the linked eLabFTW experiment, not the Automation Job resource.

### Assay

Human scientific narrative: purpose, sample/control summary, selected analyzed
results, and conclusions. The bridge records the Automation Job metadata (job
ID and execution summary) in the experiment HTML body, but does not create
eLabFTW item links. The Run Builder creates one new Assay per submitted run by
default.

## Canonical JSON contracts

Canonical specs live as attached JSON files on the corresponding eLabFTW
objects:

| Attachment | On object | Schema name |
|---|---|---|
| `method.json` | Method resource | `wallac.method.v1` |
| `layout.json` | Plate Layout resource *or* Automation Job (one-off) | `wallac.layout.v1` |
| `analysis.json` | Analysis Plan resource | `wallac.analysis.v1` |
| `job.json` | Automation Job resource | `wallac.job.v1` |

eLabFTW metadata mirrors only summary/search fields and signed hash/attachment
identity. The backend, not the browser, is the canonicalization authority.

### Deterministic serialization

- UTF-8 bytes
- sorted keys
- no insignificant whitespace
- explicit `schema_name` and `schema_version`
- SHA-256 computed over exact attached bytes
- bridge downloads the attachment by the caller-supplied ID, hashes bytes,
  compares to the caller-supplied hash, and only then parses JSON

The bridge accepts only explicitly supported schema versions. Unknown/future
versions fail closed with `SCHEMA_UNSUPPORTED`. Schema migrations create new
draft objects/attachments and new signatures; they never silently convert signed
JSON in place.

## Signature binding metadata

The metadata fields below are populated during the eLabFTW signing workflow.
The bridge does not verify eLabFTW cryptographic signatures; it compares
downloaded bytes against the caller-supplied hash (see
[Canonical JSON contracts](#canonical-json-contracts)).

| Object | Metadata fields |
|---|---|
| Method | `method_json_attachment_id`, `method_hash` |
| reusable Layout | `layout_json_attachment_id`, `layout_hash` |
| Analysis Plan | `analysis_json_attachment_id`, `analysis_hash` |
| Automation Job | `job_json_attachment_id`, `job_hash`, referenced object IDs/hashes, one-off layout hash/attachment ID when applicable |

Replacing an attachment after signing fails closed unless a new signature is
created.

## Lifecycle and versioning

Shared lifecycle model for executable objects:

| State | Selectable for new jobs | Executable |
|---|---|---|
| `draft` | no | no |
| `signed/active` | yes | yes |
| `superseded` | no | no (v1) |
| `rejected` | no | no |
| `archived` | no | no |
| `revoked` | no | no |

Signed Method/Layout/Analysis objects are immutable. Editing a signed object
creates a new draft clone/version with lineage fields:

- object kind
- version
- lifecycle status
- parent object ID
- supersedes object ID
- content hash
- canonical JSON attachment ID

Automation Jobs bind to specific signed object versions by ID and hash. They
must never resolve `latest active` at execution time. The caller supplies the
exact referenced hashes in the job request; the bridge compares downloaded
bytes against those refs before MDB generation.

## Signing and authorization

Signing is for **audit trail and provenance**, not a runtime gate. In the
direct-submit model, the Run Builder submits jobs directly to the bridge via
`POST /jobs` with reference hashes from signed eLabFTW objects. The bridge
compares downloaded attachment bytes to the caller-supplied hashes and validates
the schema version before execution.

Required signatures for generated-protocol execution (eLabFTW authoring convention — not enforced by the bridge at runtime):

1. Method
2. reusable Plate Layout (if used)
3. Analysis Plan
4. Automation Job (signed last)

For one-off layouts, `layout.json` is attached to and covered by the Automation
Job signature rather than a separate Layout resource signature.

Signing order:

1. Create or select draft Method, Layout, and Analysis Plan.
2. Finalize canonical JSON attachments and hashes.
3. Require signatures on Method, reusable Layout if used, and Analysis Plan.
4. Create Automation Job referencing exact signed object IDs and hashes.
5. Include one-off layout hash/attachment directly in `job.json` when applicable.
6. Require the Automation Job signature last.

The Wallac bridge/designer service identity may create/update drafts, attach
canonical JSON, update metadata summaries/hashes, and write back results, but it
must not count as an authorized human/operator signer for executable approval.

## Execution modes

Two distinct execution modes:

| Mode | Description | Requires signed canonical JSON |
|---|---|---|
| `generated_protocol` | New strict v1 authoring path | Yes — `job.json`, `method.json`, `layout.json`, `analysis.json` |
| `existing_protocol` | Legacy/advanced compatibility path | No — runs pre-existing Wallac/OEM protocols by signed Automation Job reference to existing protocol name or `AssayProtID` |

The main Run Builder creates `generated_protocol` jobs only.
Existing-protocol execution remains advanced/operator/debug compatibility and
must not claim Method/Layout/Analysis lineage unless those signed objects are
actually present.

Manual metadata-only jobs cannot trigger generated MDB authoring. Generated
authoring requires signed canonical `job.json`.

## Designer links

Rich Method/Layout/Analysis/Run Builder UIs live outside eLabFTW in this repo.
eLabFTW stores links that open designers in a new tab. No iframe/embed is
required or assumed (eLabFTW's HTMLPurifier strips `iframe`, `script`,
`object`, `embed`, and `form`).

Each resource category includes a `Designer URL` metadata field that the Run
Builder populates with a direct link to the external designer for that object.

## Auth and secrets

- Designer and Run Builder require authenticated operator access.
- Browser never receives the eLabFTW API key.
- Browser never receives the vm-agent bearer token.
- Browser talks to the Linux-side Wallac service only.
- Wallac service talks to eLabFTW with its service identity.
- Wallac service talks to vm-agent using configured URL/token.
- vm-agent remains private hardware/MDB adapter behind the bridge.

Browser validation is advisory only. The backend repeats all executable
validation. The backend finalizes canonical JSON and computes hashes.

## eLabFTW metadata schemas

The eLabFTW repo's `seed_wallac.py` defines the metadata schema for each
category/template. The schemas include:

### Method metadata

| Field | Type | Owner | Description |
|---|---|---|---|
| Lifecycle state | select | operator | draft, signed/active, superseded, rejected, archived, revoked |
| Measurement mode | select | operator | photometry, fluorometry, luminescence |
| Method hash | text | bridge | SHA-256 of canonical `method.json` |
| Method JSON attachment ID | text | bridge | eLabFTW upload ID of `method.json` |
| Designer URL | url | bridge | Link to external Method designer |
| Version | text | operator | Version label |
| Parent object ID | text | operator | Lineage: parent resource ID |
| Supersedes object ID | text | operator | Lineage: superseded resource ID |

### Plate Layout metadata

| Field | Type | Owner | Description |
|---|---|---|---|
| Lifecycle state | select | operator | draft, signed/active, superseded, rejected, archived, revoked |
| Plate format | select | operator | 96-well (v1 only) |
| Layout type | select | operator | reusable, one-off |
| Layout hash | text | bridge | SHA-256 of canonical `layout.json` |
| Layout JSON attachment ID | text | bridge | eLabFTW upload ID of `layout.json` |
| Designer URL | url | bridge | Link to external Layout designer |
| Version | text | operator | Version label |
| Parent object ID | text | operator | Lineage: parent resource ID |
| Supersedes object ID | text | operator | Lineage: superseded resource ID |

### Analysis Plan metadata

| Field | Type | Owner | Description |
|---|---|---|---|
| Lifecycle state | select | operator | draft, signed/active, superseded, rejected, archived, revoked |
| Analysis hash | text | bridge | SHA-256 of canonical `analysis.json` |
| Analysis JSON attachment ID | text | bridge | eLabFTW upload ID of `analysis.json` |
| Designer URL | url | bridge | Link to external Analysis designer |
| Version | text | operator | Version label |
| Parent object ID | text | operator | Lineage: parent resource ID |
| Supersedes object ID | text | operator | Lineage: superseded resource ID |

### Automation Job metadata

In the direct-submit model, the Automation Job is an **archive and audit record**,
not a runtime queue item. The bridge manages runtime state internally and writes
results to an eLabFTW experiment. The metadata schema is simplified — no
bridge-managed runtime fields:

| Group | Field | Type | Owner | Description |
|---|---|---|---|---|
| Request | Automation service | select | operator | wallac_victor2 |
| Request | Execution mode | select | operator | generated_protocol, existing_protocol |
| Request | Linked experiment ID | text | operator | eLabFTW experiment ID (where results were written) |
| Request | Protocol name | text | operator | existing_protocol: Wallac protocol name/id |
| Request | Method reference | text | operator | generated_protocol: signed Method resource ID |
| Request | Method hash | text | operator | generated_protocol: signed Method hash |
| Request | Layout reference | text | operator | generated_protocol: signed Layout resource ID (reusable) or "one-off" |
| Request | Layout hash | text | operator | generated_protocol: signed Layout hash (reusable or one-off) |
| Request | Analysis reference | text | operator | generated_protocol: signed Analysis Plan resource ID |
| Request | Analysis hash | text | operator | generated_protocol: signed Analysis Plan hash |
| Request | Expected outputs | text | operator | Expected measurement outputs |
| Bundle | Job JSON attachment ID | text | bridge | eLabFTW upload ID of `job.json` |
| Bundle | Job hash | text | bridge | SHA-256 of canonical `job.json` |
| Wallac | Wallac run ID | text | bridge | Instrument run identifier |
| Wallac | Device identity | text | bridge | Device name/version |

Removed fields (managed internally by the bridge, not in eLabFTW metadata):

| Field | Reason |
|---|---|
| `Requested action` | Submit/abort are HTTP calls to the bridge, not eLabFTW metadata changes |
| `Automation state` | Bridge tracks state in-memory |
| `Claimed by` / `Claimed at` | Bridge doesn't claim — it receives |
| `Last heartbeat` | Bridge health is independent of eLabFTW metadata |
| `Progress percent` / `Current step` | Progress is reported via the bridge HTTP API (GET /jobs/{job_id}), not eLabFTW |
| `Generated AssayProtID` | Runtime-only; exists in JobManager state, not persisted in eLabFTW metadata |
| `Final state` / `Result summary` | Successful: written to eLabFTW experiment. Failures: in-memory JobManager only. Not Automation Job metadata |
| `Last error code` / `Operator hint` | In-memory JobManager only. Not written to eLabFTW experiment or Automation Job metadata |

### Automation Job lifecycle states

In the direct-submit model, the bridge manages runtime state internally.
Only successful runs write outcome and result summary to the linked eLabFTW
experiment; failures remain in in-memory JobManager only. The simplified
state set is:

```
accepted → running → completed | failed | aborted → unknown_requires_operator_review
```

- `accepted` — job received and queued
- `running` — execution in progress
- `completed` — execution succeeded, results written to experiment
- `failed` — execution failed before instrument work
- `aborted` — execution halted by operator
- `unknown_requires_operator_review` — ambiguous state after restart or partial failure

States are tracked by the bridge in-memory and reconciled with the eLabFTW
experiment record (not the Automation Job resource metadata) for audit.

## Generated MDB protocol model

A temporary clone protocol is created for the run, started, and cleaned up
after execution. The clone's AssayProtID exists only in bridge runtime state
(JobManager), not persisted on the Automation Job.

- Temporary clone per run, not a persistent generated protocol
- Name format: `ELAB-Run-{new_id}` where `new_id` is derived from
  `int(time.time()) % 100000 + 2001000`
- Clone is cleaned up after execution (deleted in a `finally` block via
  `_cleanup_cloned_protocol()`)
- Factory template is never modified; the original template ID is used
  for name resolution, the clone ID only for starting the run
- Execute by stored numeric `AssayProtID`, not by name
- Never automatically reuse generated protocols or IDs
- ID namespace: reserve high range starting at `2000000`
- Collision-check `AssayProtocol.AssayProtID` before insert

## See also

- `docs/plans/wallac-protocol-authoring.md` — full 7-stage implementation plan
- `docs/api-reference.md` — vm-agent REST API, bridge HTTP API (POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/abort, GET /health)
- `docs/auth-secrets-policy.md` — auth and secrets policy
- `docs/abort-recovery.md` — abort and recovery semantics
- `docs/architecture-direct-submit.md` — architecture decision for direct-submit model
- eLabFTW repo: `docs/wallac-plate-reader-integration.md` — ELN-facing workflow
- eLabFTW repo: `tools/elab-seed/seed_wallac.py` — template seeder
