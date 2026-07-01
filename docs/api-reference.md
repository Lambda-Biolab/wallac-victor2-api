# API reference

REST/JSON API exposed by `vm-agent/agent.py`. All responses are JSON unless
noted. The agent binds to port **8420** on the VM's libvirt NAT interface.

## Authentication

If a token file is present on the VM (`TOKEN_FILE` in `agent.py`), every request
must carry it:

```
Authorization: Bearer <token>
```

Missing/incorrect token → `401 {"error": "unauthorized"}`. If no token file
exists, auth is disabled (the libvirt NAT is host-only) and the agent logs a
warning at startup.

## Conventions

- **State codes** come straight from the OEM COM server; `state` is the matching
  human-readable text.
- **Run lifecycle:** `starting → running → measured` (or `→ aborted` / `→ failed`).
- **Errors** share one shape — a machine `error` code plus a human `hint`:
  ```json
  {"error": "instrument_not_ready",
   "hint": "close the lid, load a plate, clear any error in MlrMgr, then retry",
   "detail": "..."}
  ```
  Common codes: `instrument_not_ready` (409 — the reader refused the run),
  `instrument_busy` (409 — a run is already active), `instrument_not_connected`
  / `instrument_link_lost` (503), `protocol_not_found` (404) /
  `protocol_ambiguous` (409, with a `candidates` list), `measure_timeout`
  (504), `unauthorized` (401), `not_found` (404, hint `see GET /docs`). A
  genuine COM fault is `503 com_error` (with `trace`); an unexpected server bug
  is `500 internal_error`.

---

## Measure (one-shot)

### `POST /measure`
The friendly entrypoint: resolve a protocol **by name**, start the run, wait for
the plate read, and return the deduped per-well OD table — in a single call.

Body:

| field | type | default | notes |
|---|---|---|---|
| `protocol` | string\|int | — | **required**; name (case-insensitive, exact then unique substring) or numeric id |
| `wait` | bool | `true` | block until measured, then return results |
| `timeout` | number | `600` | seconds to wait when `wait=true` |
| `shape` | `list`\|`grid` | `list` | also include an `{well: value}` grid map |
| `value` | `od`\|`raw` | `od` | grid cell value |
| `dry_run` | bool | `false` | validate the run definition only; no carrier movement |

- `wait=true` → `200` with the persisted, deduped results:
  ```json
  {
    "run_id": "r-c4279c9438e5", "assay_id": 24, "state": "measured",
    "protocol": {"id": 2000000, "name": "Absorbance @ 600 (1.0s)", "group": "Photometry"},
    "source": "persisted", "well_count": 8,
    "wells": [{"well": "A01", "od": 0.071, "counts": 360344}],
    "grid": {"A01": 0.071}
  }
  ```
- `wait=false` → `202 {"run_id": "...", "state": "running", "protocol": {...}}`.
- `dry_run=true` → `200 {"dry_run": true, "protocol_id": ..., "protocol": {...}}`.
- If the reader refuses to start → `409 instrument_not_ready` (with a `hint`);
  if a run is already active → `409 instrument_busy`.

### `GET /docs`
A JSON catalog of every endpoint (method, path, purpose, and the `/measure`
body) plus the canonical `well` object — handy for discovery. Also served at `/`.

---

## Status & instrument

### `GET /health`
Liveness + whether the instrument link is up.

`200` when connected, `503` when not:
```json
{
  "instrument_connected": true,
  "state": "Idle",
  "state_code": 0,
  "is_running": false,
  "is_error": false,
  "ok": true,
  "ts": "2026-06-03T10:12:00+00:00"
}
```

### `GET /status`
Latest snapshot captured by the background monitor (no live COM call). `200`
when connected, else `503`:
```json
{
  "ts": "2026-06-03T10:12:00+00:00",
  "connected": true,
  "state": "Idle",
  "state_code": 0,
  "is_running": false,
  "is_error": false,
  "is_idle": true,
  "target_temperature": 25.0,
  "seq": 1423
}
```
On a failed poll: `{"connected": false, "error": "<ExcType>: <msg>", ...}`.

### `GET /monitor`
Server-Sent Events stream (`Content-Type: text/event-stream`). One event is
pushed whenever the monitor snapshot changes (~1 Hz):
```
data: {"seq": 1424, "ts": "...", "connected": true, "state": "Running", ...}
```

### `GET /instrument`
Static-ish instrument identity and capabilities (identity fields are only read
when connected):
```json
{
  "connected": true,
  "target_temperature": 25.0,
  "plate_heating": false,
  "serial": 4200123,
  "model": 1420,
  "technologies": {
    "tr_fluorometer": true,
    "prompt_fluorometer": true,
    "photometer": true,
    "luminometer": true,
    "barcode_reader": false,
    "temp_control": true,
    "dispenser": false
  }
}
```

### `GET /protocols`
Assay protocols read from the instrument database. Cached; pass `?refresh=1` to
reload, or `?q=<text>` to filter by name (case-insensitive).
```json
{
  "count": 2,
  "protocols": [
    {"id": 1000003, "name": "Absorbance 405", "number": 3,
     "version": 1, "group": "Photometry", "factory_preset": true}
  ]
}
```

### `GET /protocols/{name|id}`
Resolve a single protocol by numeric id **or name** (case-insensitive: exact
match, then unique substring). Returns the protocol record, or
`404 protocol_not_found` / `409 protocol_ambiguous` (the latter with a
`candidates` list) — so callers never have to hard-code the magic integer ids.

---

## Runs (live assays)

### `POST /runs`
Start an assay. Body:

| field | type | notes |
|---|---|---|
| `protocol` | string\|int | **required** — protocol name or id (`protocol_id` still accepted) |
| `dry_run` | bool | validate only; does not move the carrier |
| `plate_id` | any | optional, echoed in run metadata |

- **Dry run** → `200`:
  ```json
  {"dry_run": true, "protocol_id": 1000003, "load_first_plate": true}
  ```
- **Real start** → `202` (this physically moves the carrier):
  ```json
  {"run_id": "r-ab12cd34ef56", "state": "running",
   "job_id": 412, "assay_id": 880, "protocol_id": 1000003}
  ```
- `400 protocol_required` if no `protocol`/`protocol_id` is given.
- `409 instrument_busy {"active_run": "<id>"}` if a run is already active.

> **`POST /measure` is the higher-level alternative** — it takes a protocol
> name, waits, and returns the OD table in one call (see above). Use
> `POST /runs` when you want to drive the run lifecycle yourself.

### `GET /runs`
```json
{"count": 1, "runs": [{"run_id": "r-ab12cd34ef56", "state": "running", ...}]}
```

### `GET /runs/{id}`
Run metadata; when active, includes a live `live` block. `404` if unknown.
```json
{
  "run_id": "r-ab12cd34ef56",
  "protocol_id": 1000003,
  "plate_id": null,
  "state": "running",
  "started_at": "2026-06-03T10:12:00+00:00",
  "ended_at": null,
  "job_id": 412,
  "assay_id": 880,
  "live": {"is_running": true, "is_measured": false, "is_ok": true,
           "state_text": "Running", "state_code": 5}
}
```

### `GET /runs/{id}/results`
Per-well results for a run. **While the run is active** this streams the live
counts buffer; **once measured** it serves the authoritative persisted rows,
deduped to one `{well, od, counts}` per well. Query: `shape=list|grid`,
`value=od|raw`, `dedup=1|0`. `source` reports which path served the data.
```json
{"run_id": "r-ab12cd34ef56", "source": "persisted", "well_count": 8,
 "wells": [{"well": "A01", "od": 0.071, "counts": 360344}],
 "grid": {"A01": 0.071}}
```

### `GET /runs/{id}/export`
The same results as CSV (`Content-Type: text/csv`); honors `shape=grid` and
`value=od|raw`:
```
well,od,counts
A01,0.071,360344
```

### `POST /runs/{id}/abort`
Cancel a run. The instrument only honors an abort once it is fully into the
measurement, so aborts are **rejected for the first 60 s**:
- Too early → `425`:
  ```json
  {"error": "too early to abort", "detail": "...", "run": "<id>", "age_s": 12.3}
  ```
- Otherwise → `200 {"ok": true, "is_running": false, "state_text": "Idle",
  "run": "<id>", "state": "aborted"}`.

### `DELETE /runs/{id}`
Forget a finished/failed/stuck run record (frees the `instrument_busy` guard
without restarting the agent). Refuses a still-running run unless `?force=1`:
```json
{"deleted": "r-ab12cd34ef56"}
```
`404 run_not_found` if unknown; `409 run_active` if running and not forced.

---

## Jobs (completed assays)

### `GET /jobs`
```json
{
  "count": 1,
  "jobs": [{
    "assay_id": 880,
    "protocol_id": 1000003,
    "protocol_name": "Absorbance 405",
    "begin": "2026-06-03 10:12:00",
    "end": "2026-06-03 10:14:30",
    "wells_x": 12,
    "wells_y": 8,
    "notes": null,
    "errors": null
  }]
}
```

### `GET /jobs/{id}`
A single job object, or `404 {"error": "no such job", "job": "<id>"}`.

### `GET /jobs/{id}/results`
Per-well results. By default **deduped** to one `{well, od, counts}` per well
(add `?shape=grid` for a grid map); pass `?dedup=0` for the full raw rows
(every `result_type`, with `meas_a`/`meas_b`/`well_id`/`label`).
```json
{"assay_id": "880", "source": "persisted", "well_count": 96,
 "wells": [{"well": "A01", "od": 0.0417, "counts": 284932}]}
```
With `?dedup=0`:
```json
{
  "assay_id": "880", "count": 192,
  "wells": [{
    "well": "A01", "well_id": 0, "plate": 1, "label": 0,
    "result_type": 0, "repeat": 1,
    "meas_a": 284932, "meas_b": 2, "od": 0.0417
  }]
}
```

> **`od` is a convenience value** computed as
> `log10((bg_count / bg_flashes) / (signal / flashes))` from the per-plate
> background. Validate it against an OEM-reported OD on a real plate before
> relying on it; `meas_a` / `meas_b` are the raw instrument counts.

### `GET /jobs/{id}/export`
CSV export. Query params:

| param | values | default |
|---|---|---|
| `format` | `long`, `grid` | `long` |
| `value` | `raw` (meas_a), `od` | `raw` |

- `format=long`:
  ```
  well,plate,label,result_type,repeat,meas_a,meas_b,od
  A01,1,0,0,1,284932,2,0.0417
  ```
- `format=grid` → 8×12 plate layout:
  ```
  row,1,2,3,4,5,6,7,8,9,10,11,12
  A,284932,,,,,,,,,,,
  ...
  H,,,,,,,,,,,,200000
  ```

---

## Admin

### `POST /admin/reconnect`
Drop and recreate the COM connection to the OEM server:
```json
{"reconnected": true}
```

### MDB endpoints (generated-protocol support)

These endpoints expose the instrument's Jet database (MDB) for the
generated-protocol authoring pipeline. All write operations are guarded by a
single-writer lock (`_mdb_write_lock`).

Read operations are always permitted. Write operations require the
`WALLAC_ENABLE_PROTOCOL_AUTHORING=true` environment variable on the vm-agent;
without it they return `403 authoring_disabled`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/mdb/groups?name=<name>` | token | Lookup a ProtocolGroup ID by exact GroupName |
| `GET` | `/mdb/protocols?name=<name>` | token | Find an AssayProtocol row by exact ProtName |
| `GET` | `/mdb/protocols/<id>` | token | Get a full AssayProtocol row by AssayProtID |
| `GET` | `/mdb/max-protocol-id` | token | Highest AssayProtID in the database |
| `POST` | `/mdb/protocols` | token + authoring | Insert a new AssayProtocol row |
| `DELETE` | `/mdb/protocols/<id>` | token + authoring | Delete an AssayProtocol row by ID |
| `POST` | `/mdb/backup` | token + authoring | Create a timestamped backup of the MDB |
| `PATCH` | `/mdb/protocols/{id}/plate_map` | token + authoring | Overwrite the 108-byte PlateMap binary blob |
| `PATCH` | `/mdb/protocols/{id}/wells` | token + authoring | Set wells by name/row/all (builds plate_map internally) |
| `POST` | `/mdb/query` | token | Execute a read-only SELECT query |

#### `GET /mdb/groups?name=<name>`

Query parameter `name` (required, case-sensitive). Returns the ProtocolGroup ID:

```json
{"group_id": 3, "name": "Photometry"}
```

`400` if name is missing; `404` if no group matches.

#### `GET /mdb/protocols?name=<name>`

Returns the full AssayProtocol row for an exact `ProtName` match, or
`404 protocol_not_found`:

```json
{
  "AssayProtID": 2000001,
  "ProtName": "ELAB-Job-1-abc12345",
  "ProtNumber": null,
  "ProtVersion": 1,
  "FactoryPreset": false,
  "ProtGroup": 3
}
```

`400` if name is missing.

#### `GET /mdb/protocols/<id>`

Returns the full AssayProtocol row by numeric AssayProtID, or
`404 protocol_not_found`. `400` if id is not an integer.

#### `GET /mdb/max-protocol-id`

```json
{"max_assay_prot_id": 2000001}
```

Returns `0` if the table is empty.

#### `POST /mdb/protocols`

Requires `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`. Acquires the write lock.

Body — JSON dict with these known columns (extra keys ignored):

| Field | Type | Required |
|-------|------|----------|
| `AssayProtID` | int | yes |
| `ProtName` | string | yes |
| `ProtNumber` | int\|null | no |
| `ProtVersion` | int | no |
| `FactoryPreset` | bool | no |
| `ProtGroup` | int | no (ProtocolGroup ID) |

Returns `201`:

```json
{"assay_prot_id": 2000001, "created": true}
```

`400` if `AssayProtID` or `ProtName` is missing; `403 authoring_disabled` if
the feature flag is not set.

#### `DELETE /mdb/protocols/<id>`

Requires `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`. Acquires the write lock.

```json
{"assay_prot_id": 2000001, "deleted": true}
```

`404 delete_failed` if the id does not exist; `403 authoring_disabled` if the
feature flag is not set.

#### `POST /mdb/backup`

Requires `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`. Acquires the write lock.

Body:

| Field | Type | Required |
|-------|------|----------|
| `name` | string | yes (backup filename) |

```json
{"backup_path": "C:\\Users\\Public\\mdb_backups\\mlr3_20260627_120000.mdb", "created": true}
```

`400` if name is missing.

#### `POST /mdb/query`

Execute an arbitrary SELECT query against the MDB. Only `SELECT` statements
are allowed; any other SQL dialect returns `400 invalid_query`.

Body:

| Field | Type | Required |
|-------|------|----------|
| `sql` | string | yes (SELECT statement) |

```json
{
  "count": 2,
  "rows": [
    {"AssayProtID": 1000003, "ProtName": "Absorbance 405", "ProtNumber": 3},
    {"AssayProtID": 1000004, "ProtName": "Absorbance @ 600 (1.0s)", "ProtNumber": 4}
  ]
}
```

`400` if sql is missing or not a SELECT statement.

#### `PATCH /mdb/protocols/{id}/plate_map`

Requires `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`. Acquires the write lock.

Overwrite the `PlateMap` binary blob — a 108-byte array (12-byte header + 96-byte
8×12 grid, row-major, `01` = measure, `00` = skip). See AGENT_LEARNINGS.md for
the binary format details.

Body:

| Field | Type | Required |
|-------|------|----------|
| `plate_map` | `[int]` | yes — exactly 108 integers |

Response (`200`):

```json
{"protocol_id": 2000001, "bytes_written": 108}
```

`400` if `plate_map` is not a list of 108 ints; `403 authoring_disabled` if the
feature flag is not set; `404 protocol_not_found` if the id does not exist.

#### `PATCH /mdb/protocols/{id}/wells`

Requires `WALLAC_ENABLE_PROTOCOL_AUTHORING=true`. Acquires the write lock.

A higher-level alternative to `PATCH /mdb/protocols/{id}/plate_map` that accepts
well names or rows instead of raw bytes. Builds the 108-byte plate_map internally.

Body — exactly one of these fields:

| Field | Type | Description |
|-------|------|-------------|
| `rows` | `[string]` | Entire rows, e.g. `["A","B"]` measures all wells in rows A and B |
| `wells` | `[string]` | Specific wells, e.g. `["A1","A2","B1","B2"]` |
| `all` | `bool` | When `true`, restores the full 96-well plate |

Response (`200`):

```json
{"protocol_id": 2000001, "bytes_written": 108}
```

`400` if none of `rows`, `wells`, or `all` is provided, or if a well name is
invalid; `403 authoring_disabled` if the feature flag is not set;
`404 protocol_not_found` if the id does not exist.

---

## Bridge HTTP API

HTTP API exposed by the bridge daemon for job submission and status queries.
In the direct-submit model, the Run Builder submits jobs here instead of
creating eLabFTW Automation Job resources and waiting for polling.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/jobs` | Submit a new job for execution |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/jobs/{id}` | Get job status and results |
| `POST` | `/jobs/{id}/abort` | Abort a running job |
| `GET` | `/health` | Bridge health check |

Auth: Bearer token in `WALLAC_DESIGNER_TOKEN` env var (disabled if unset).

### `POST /jobs` — submit job

Body:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | Human-readable job title |
| `execution_mode` | string | yes | `existing_protocol` or `generated_protocol` |
| `protocol_name` | string | yes | Wallac protocol name/id |
| `elabftw_experiment_id` | int | no | Link results to an existing eLabFTW experiment |
| `method_ref` | object | no | `{object_id, hash, attachment_id}` — signed Method reference |
| `layout_ref` | object | no | `{source, object_id, hash, attachment_id}` — signed Layout reference |
| `analysis_ref` | object | no | `{object_id, hash, attachment_id}` — signed Analysis Plan reference |

Response (`202`):

```json
{"job_id": "job-abc123", "status": "accepted"}
```

The bridge executes the job asynchronously. Status can be polled via
`GET /jobs/{job_id}`.

### `GET /jobs` — list jobs

```json
{
  "count": 2,
  "jobs": [
    {"job_id": "job-abc123", "status": "accepted", "title": "OD600 run", "created_at": "..."},
    {"job_id": "job-def456", "status": "completed", "title": "Fluorescence assay", "created_at": "..."}
  ]
}
```

### `GET /jobs/{id}` — job status

```json
{
  "job_id": "job-abc123",
  "status": "running",
  "title": "OD600 run — E. coli growth",
  "execution_mode": "existing_protocol",
  "protocol_name": "Absorbance @ 600 (1.0s)",
  "wallac_run_id": "r-ab12cd34ef56",
  "elabftw_experiment_id": 42,
  "progress_percent": 55.0,
  "current_step": "Measuring...",
  "started_at": "2026-06-27T10:12:00+00:00",
  "elapsed_seconds": 30.0,
  "error": "",
  "operator_hint": "",
  "result_summary": "",
  "artifacts": []
}
```

`404` if the job ID is unknown.

### `POST /jobs/{id}/abort` — abort job

Request a controlled abort of a running job. The bridge forwards the request to
the vm-agent.

```json
{"ok": true}
```

`404` if the job is not active. Subject to the vm-agent's 60-second minimum
abort age (returns `425` if too early).

### `GET /health` — bridge health

```json
{"status": "ok", "instrument_connected": true, "uptime_seconds": 3600}
```

---

## Designer API

FastAPI application served by the bridge daemon for the Run Builder UI and
eLabFTW draft authoring. See `bridge/designer_app.py` and
`bridge/designer.py`.

| Property | Value |
|----------|-------|
| Base URL | `http://<host>:8422` (configurable, no default) |
| Auth | Bearer token in `WALLAC_DESIGNER_TOKEN` env var (disabled if unset) |
| OpenAPI docs | `GET /docs` (FastAPI Swagger UI) |

### `GET /health`

```json
{"status": "ok"}
```

### `GET /run-builder`

Serves the Run Builder single-page HTML application (`bridge/run_builder.html`).
`404` if the HTML file is not found.

### CRUD endpoints (all four kinds)

The same CRUD pattern applies for **methods**, **layouts**, **analyses**, and
**jobs**. Replace `{kind}` with one of `method`, `layout`, `analysis`, `job`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/{kind}s` | Create a new draft |
| `GET` | `/api/{kind}s` | List all drafts |
| `GET` | `/api/{kind}s/{item_id}` | Get a draft |
| `PATCH` | `/api/{kind}s/{item_id}` | Update draft spec |
| `POST` | `/api/{kind}s/{item_id}/finalize` | Finalize (canonicalize + attach JSON + write hash) |
| `POST` | `/api/{kind}s/{item_id}/clone` | Clone a signed object to a new draft |

**Auth:** all CRUD endpoints require a valid Bearer token if
`WALLAC_DESIGNER_TOKEN` is set.

#### `POST /api/{kind}s` — create draft

Body:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Human-readable title for the eLabFTW item |
| `spec` | dict | Spec dict (schema-validated on finalize) |

Response (`201`):

```json
{
  "item_id": 42,
  "title": "My Method",
  "category_id": 7,
  "lifecycle": "draft",
  "spec": {},
  "hash": "",
  "json_attachment_id": 0
}
```

#### `GET /api/{kind}s` — list drafts

```json
[
  {
    "item_id": 42,
    "title": "My Method",
    "category_id": 7,
    "lifecycle": "draft",
    "spec": {},
    "hash": "",
    "json_attachment_id": 0
  }
]
```

#### `GET /api/{kind}s/{item_id}` — get draft

Single `DraftResponse` object (same shape as above). `404` if not found.

#### `PATCH /api/{kind}s/{item_id}` — update draft spec

Body:

| Field | Type | Description |
|-------|------|-------------|
| `spec` | dict | Updated spec dict (replaces existing entirely) |

Returns the updated `DraftResponse`. `403` if the item is not in draft state
(only signed objects can be used as templates, via `/clone`).

#### `POST /api/{kind}s/{item_id}/finalize` — finalize

Canonicalizes the spec dict into JSON, computes the SHA-256 hash, uploads the
JSON as an eLabFTW attachment, and writes hash + attachment ID into metadata.
The object remains in `draft` state until the operator signs it in the eLabFTW
UI.

Response (`200`):

```json
{
  "item_id": 42,
  "hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "json_attachment_id": 17,
  "filename": "method.json"
}
```

`403` if the item is not in draft state.

#### `POST /api/{kind}s/{item_id}/clone` — clone signed object

Creates a new draft from a signed/active object (immutable source). Body:

| Field | Type | Description |
|-------|------|-------------|
| `new_title` | string | Title for the new draft |

Returns a `DraftResponse` for the new draft (lifecycle: `draft`). `403` if the
source object is not in `signed_active` lifecycle state.

---

## Bridge daemon

The bridge daemon (`main.py`) runs on the Linux host and orchestrates the
end-to-end job lifecycle via the direct-submit model:

1. **Accept** jobs via HTTP `POST /jobs` (no eLabFTW polling)
2. **Verify** cryptographic signatures on job requests
3. **Execute** — resolve/measure via the vm-agent (either an existing protocol
   or a generated protocol written to the MDB)
4. **Write back** results (CSV exports, artifacts) to an eLabFTW experiment

It also runs:
- A **dashboard server** with SSE live progress (default `0.0.0.0:8421`)
- The **bridge HTTP API** for job submission and status queries

### Configuration

All configuration is via environment variables (see `bridge/config.py`).

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLAC_ELABFTW_URL` | `https://localhost:3148` | eLabFTW server URL |
| `WALLAC_ELABFTW_API_KEY` | _(required)_ | Dedicated bridge API key (for write-back only; no polling required) |
| `WALLAC_ELABFTW_CATEGORY` | `9` | Automation Job resource category ID |
| `WALLAC_VM_AGENT_URL` | `http://192.168.122.203:8420` | vm-agent REST API base |
| `WALLAC_VM_AGENT_TOKEN` | `""` | vm-agent bearer token |
| `WALLAC_DASHBOARD_HOST` | `0.0.0.0` | Dashboard listen address |
| `WALLAC_DASHBOARD_PORT` | `8421` | Dashboard listen port |
| `WALLAC_DASHBOARD_TOKEN` | `""` | Dashboard auth token (unset = open on LAN) |
| `WALLAC_DEVICE_IDENTITY` | `victor2-unknown` | Instrument identity for diagnostics |
| `WALLAC_SPOOL_DIR` | `/var/lib/wallac-bridge/spool` | Result write-back spool directory |
| `WALLAC_ENABLE_PROTOCOL_AUTHORING` | `""` | Feature flag: set `true` to enable generated protocols |
| `WALLAC_DESIGNER_TOKEN` | `""` | Bearer token for the Designer API (unset = no auth) |

Removed variables (no longer needed in direct-submit model):

| Variable | Reason |
|---|---|
| `WALLAC_BRIDGE_IDENTITY` | No claiming — jobs are received, not claimed |
| `WALLAC_POLL_INTERVAL` | No polling — jobs arrive via HTTP POST |

### Deployment

The bridge runs as a systemd service. See `deploy/wallac-bridge.service` and
`deploy/bridge.env.example`:

```
[Unit]
Description=Wallac Victor2 eLabFTW Bridge
After=network-online.target

[Service]
Type=simple
User=antonio
EnvironmentFile=/etc/wallac-bridge/bridge.env
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10
```

The env file at `/etc/wallac-bridge/bridge.env` holds all secrets and is
**never** committed to the repository.

### Dashboard API

Served on `WALLAC_DASHBOARD_HOST:WALLAC_DASHBOARD_PORT` (default
`0.0.0.0:8421`). Auth via `WALLAC_DASHBOARD_TOKEN` (Bearer token). If the
token is unset, the dashboard is open on the LAN.

#### `GET /` — dashboard HTML

The single-page dashboard UI (`bridge/dashboard.html`).

#### `GET /api/jobs/{id}` — job state JSON

Returns the full job state object for the dashboard panels:

```json
{
  "item_id": 412,
  "title": "Measure plate A",
  "experiment_id": "",
  "wallac_run_id": "r-ab12cd34ef56",
  "requester": "operator@lab",
  "operator": "wallac-bridge",
  "device_identity": "victor2-1420",
  "service_version": "0.1.0",
  "state": "running",
  "progress_percent": 55.0,
  "current_step": "Measuring...",
  "started_at": "2026-06-27T10:12:00+00:00",
  "last_heartbeat": "2026-06-27T10:12:30+00:00",
  "elapsed_seconds": 30.0,
  "protocol_name": "Absorbance @ 600",
  "plate_layout_reference": "",
  "expected_outputs": "CSV with OD values",
  "request_checksum": "",
  "preflight": [{"name": "Lid closed", "passed": true, "detail": ""}],
  "plate_wells": {},
  "event_log": ["Job claimed", "Protocol resolved", "Measurement started"],
  "artifacts": [{"filename": "results.csv", "comment": "OD table"}],
  "result_summary": "",
  "writeback_status": "pending",
  "writeback_last_retry": "",
  "writeback_operator_hint": "",
  "last_error_code": "",
  "operator_hint": ""
}
```

`404` if the job is unknown.

#### `GET /api/jobs/{id}/stream` — SSE live progress

Server-Sent Events stream (`Content-Type: text/event-stream`) that pushes the
full job state dict on every change. The browser `EventSource` auto-reconnects
on interruption using the `retry: 3000` hint. Heartbeat comments (`: heartbeat`)
are sent every second when no state change occurs. The stream ends when the job
reaches a terminal state (`completed`, `failed`, `aborted`, or
`unknown_requires_operator_review`).

#### `POST /api/jobs/{id}/abort` — request abort

Request a controlled abort of a running job. The bridge forwards the request to
the vm-agent via the bridge HTTP API (`POST /jobs/{id}/abort`). No eLabFTW
metadata change or polling required.

```json
{"ok": true}
```

`404` if the job is not active.

#### `GET /api/jobs/{id}/artifacts/{filename}` — download artifact

Download a result artifact (CSV, image, etc.) produced by the job execution.
Content-Type is determined by the artifact. `404` if the artifact does not
exist.

---

## Error codes

### vm-agent error codes

Returned by the instrument microservice (`agent.py`). These are the standard
codes produced by `_classify_exc()` and the various endpoint handlers.

| HTTP | Code | Description | Hint |
|------|------|-------------|------|
| 401 | `unauthorized` | Missing or invalid bearer token | Send `Authorization: Bearer <token>` |
| 404 | `not_found` | Path does not match any endpoint | See `GET /docs` |
| 404 | `protocol_not_found` | No protocol matches the given name or id | `GET /protocols` to list |
| 404 | `run_not_found` | No run with that id | `GET /runs` to list |
| 404 | `group_not_found` | No ProtocolGroup matches the name | Provide a valid GroupName |
| 404 | `delete_failed` | AssayProtocol ID not found for deletion | Check the ID exists |
| 409 | `instrument_not_ready` | Reader refused to start | Close lid, load a plate, clear MlrMgr errors |
| 409 | `instrument_busy` | A run is already active | Wait for it, or abort/DELETE it first |
| 409 | `protocol_ambiguous` | Name matches several protocols | Use a more specific name or numeric id |
| 409 | `run_active` | Run is running and not forced | Abort first or pass `?force=1` |
| 425 | `too early to abort` | Abort requested <60 s into the run | Wait and retry |
| 500 | `internal_error` | Unexpected server bug | Check agent stderr log |
| 503 | `instrument_not_connected` | MlrMgr not connected to the reader | Start OEM GUI and wait for connection |
| 503 | `instrument_link_lost` | COM link dropped | `POST /admin/reconnect` |
| 503 | `com_error` | Unexpected COM/instrument error | Check agent stderr log |
| 504 | `measure_timeout` | Run did not finish within the timeout | Poll `GET /runs/{id}` |
| — | `com_timeout` | Instrument did not respond in time | `POST /admin/reconnect` |
| — | `authoring_disabled` | Write operation blocked by feature flag | Set `WALLAC_ENABLE_PROTOCOL_AUTHORING=true` |
| — | `name_required` | Query parameter `name` is missing | Provide `?name=<value>` |
| — | `sql_required` | Request body missing `sql` field | Provide `{"sql": "SELECT ..."}` |
| — | `invalid_query` | SQL is not a SELECT statement | Only SELECT queries are allowed |
| — | `missing_fields` | Required fields missing from POST body | Provide `AssayProtID` and `ProtName` |
| — | `invalid_id` | `id` path parameter is not an integer | Use a numeric ID |

### Bridge error codes

Structured errors raised by the bridge daemon components (validation, execution,
designer). These share a uniform JSON shape:

```json
{
  "code": "signature_verification_failed",
  "severity": "error",
  "human_message": "Signature verification failed for Automation Job 42",
  "operator_hint": "The signature archive may be corrupted or the signing key may have been revoked.",
  "retryable": false,
  "details": {"item_id": 42, "upload_id": 7}
}
```

#### Validation errors (signature & canonical spec)

| Code | Severity | Description | Retryable |
|------|----------|-------------|-----------|
| `unsigned_job` | error | Job has no signature archive | no |
| `signature_verification_failed` | error | Cryptographic signature is invalid | no |
| `request_modified_after_signature` | error | Request fields changed after signing | no |

#### Canonical spec errors

| Code | Severity | Description | Retryable |
|------|----------|-------------|-----------|
| `canonical_hash_mismatch` | error | Downloaded attachment SHA-256 does not match signed hash | no |
| `canonical_attachment_mismatch` | error | Attachment metadata does not match | no |
| `schema_unsupported` | error | Schema name or version is not in the supported set | no |
| `signature_missing` | error | No signature archive found on the eLabFTW item | no |
| `signature_invalid` | error | Cryptographic signature verification failed | no |
| `signer_unauthorized` | error | Signing key is not in the authorized set | no |
| `referenced_object_not_active` | error | Referenced method/layout/analysis is not in signed_active state | no |
| `capability_unavailable` | warning | Required instrument capability is not available | yes |
| `mode_not_enabled` | warning | Requested measurement mode is not enabled | no |
| `template_missing_or_drifted` | error | eLabFTW template category missing or schema changed | no |

#### MDB & generated-protocol errors

| Code | Severity | Description | Retryable |
|------|----------|-------------|-----------|
| `mdb_id_collision` | error | Generated AssayProtID collides with existing row | no |
| `mdb_backup_failed` | error | MDB backup could not be created | yes |
| `mdb_write_failed` | error | MDB insert/delete failed | yes |
| `post_write_verification_failed` | error | Verification read after write did not match | yes |

#### Execution & write-back errors

| Code | Severity | Description | Retryable |
|------|----------|-------------|-----------|
| `result_incomplete` | warning | Measurement completed but results are partial | yes |
| `analysis_failed` | error | Post-measurement analysis step failed | yes |
| `writeback_spooled` | info | Result write-back could not complete; spooled for retry | yes |
| `operator_review_required` | error | Bridge cannot proceed automatically; operator must inspect | no |
| `invalid_transition` | error | Invalid job state transition (bridge bug) | no |

#### vm-agent proxied errors

The bridge also surfaces vm-agent errors directly. When a vm-agent call fails,
the error code and hint are forwarded to eLabFTW metadata and the job state.
See the "vm-agent error codes" section above for the full list.
