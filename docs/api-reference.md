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
- **Run lifecycle:** `starting → running → measured` (or `→ aborted`).
- **Errors:** an unexpected COM failure returns `503` with
  `{"error": "com_error", "detail": "...", "trace": "..."}`. Unknown routes
  return `404 {"error": "not found", "path": "..."}`.

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
reload.
```json
{
  "count": 2,
  "protocols": [
    {"id": 1000003, "name": "Absorbance 405", "number": 3,
     "version": 1, "group": "Photometry", "factory_preset": true}
  ]
}
```

---

## Runs (live assays)

### `POST /runs`
Start an assay. Body:

| field | type | notes |
|---|---|---|
| `protocol_id` | int | **required** |
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
- `400` if `protocol_id` is missing.
- `409 {"error": "instrument busy", "active_run": "<id>"}` if a run is already
  active.

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
Live per-well counts for the current run (empty when idle):
```json
{"run": "r-ab12cd34ef56",
 "wells": [{"well": "A01", "counts": 284932, "result_type": 0,
            "plate": 1, "plate_repeat": 1}]}
```

### `GET /runs/{id}/export`
Same live data as CSV (`Content-Type: text/csv`):
```
well,counts,result_type,plate,plate_repeat
A01,284932,0,1,1
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
Per-well results with computed optical density:
```json
{
  "assay_id": "880",
  "count": 96,
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
