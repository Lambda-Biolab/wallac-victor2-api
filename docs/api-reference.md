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
