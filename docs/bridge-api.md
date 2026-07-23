# Bridge & designer API reference

HTTP API exposed by the bridge (`bridge/bridge_app.py`, port **8423`) and the
Run Builder designer (`bridge/designer_app.py`, port **8422**) on the Linux
host. For the vm-agent (`:8420`) contract, see
[`api-reference.md`](api-reference.md).

## Authentication

Both FastAPI apps use optional `Authorization: Bearer <token>`:

- The **bridge** reads from `WALLAC_BRIDGE_TOKEN`.
- The **designer** reads from `WALLAC_DESIGNER_TOKEN`.

(The vm-agent on the Windows VM authenticates separately via a token file on
disk; see [`api-reference.md`](api-reference.md).)

If unset, auth is disabled with no warning. **Never commit real tokens** —
tracked templates (`deploy/bridge.env.example`) use placeholder values.
See [`auth-secrets-policy.md`](auth-secrets-policy.md) for the full policy.

> **Run Builder single-token limitation.** The Run Builder UI reuses the same
> bearer token for both designer and bridge requests. Operators using the UI
> must set `WALLAC_BRIDGE_TOKEN` and `WALLAC_DESIGNER_TOKEN` to matching values.
> Direct API clients may use separate tokens per service.

## bridge API — `:8423`

| Method & path | Purpose |
|---|---|
| `GET /health` | bridge liveness + worker status + current job |
| `POST /jobs` | submit a job for execution (idempotent: duplicate spec → `409`; see `wells_spec` contract below) |
| `GET /jobs` | list all jobs |
| `GET /jobs/{job_id}` | job status, events, artifacts, live wells |
| `POST /jobs/{job_id}/abort` | abort a running job — see [`abort-recovery.md`](abort-recovery.md) for state-machine semantics, race-window guarantees, and incident recovery |

## designer API — `:8422`

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /config` | returns `{elabftw_url, bridge_url}` for the Run Builder (explicitly omits `vm_agent_url` — security) |
| `GET /run-builder` | Run Builder single-page app |
| `GET /elabftw/events?items_id=&start=&end=` | eLabFTW events API proxy; requires designer bearer auth (self-signed cert workaround) |
| `POST/GET /api/{methods\|layouts\|analyses\|jobs}` | create / list drafts |
| `GET/PATCH /api/{...}/{item_id}` | read / update a draft |
| `POST /api/{...}/{item_id}/finalize` | canonicalize + hash + attach JSON |
| `POST /api/{...}/{item_id}/clone` | clone a signed object to a new draft |

For the draft/signed lifecycle and canonical JSON schemas, see
[`elabftw-object-model.md`](elabftw-object-model.md).

### `POST /jobs` — `wells_spec` contract

The request body carries an optional `wells_spec` field that is a plate-map
override (`{"all": true}`, `{"rows": ["A","B"]}`, or `{"wells": ["A1","A2"]}`).

- **`existing_protocol`** — the bridge clones the resolved factory protocol
  into a per-run id, applies the override on the clone via
  `PATCH /mdb/protocols/{id}/wells`, runs on the clone, and deletes the clone
  in `finally` so the factory preset is never written to. An invalid well
  name (e.g. `Z9`) is accepted at the bridge boundary and surfaces as a job
  failure after the vm-agent rejects the PATCH. Omit `wells_spec` (or pass
  `{}`) to run the protocol's factory 96-well plate map unchanged.
- **`generated_protocol`** — plate map is derived from the signed
  `layout_ref` spec; `wells_spec` is accepted (currently unused) to preserve
  forward compatibility.

The clone-then-PATCH-then-cleanup path lives in
`bridge/executor.py::BridgeExecutor._execute_existing_protocol` (and the
shared `VmAgentClient.set_protocol_wells` helper that calls the vm-agent's
`PATCH /mdb/protocols/{id}/wells` endpoint).
