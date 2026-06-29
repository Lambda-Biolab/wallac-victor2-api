# Bridge & designer API reference

HTTP API exposed by the bridge (`bridge/bridge_app.py`, port **8423`) and the
Run Builder designer (`bridge/designer_app.py`, port **8422**) on the Linux
host. For the vm-agent (`:8420`) contract, see
[`api-reference.md`](api-reference.md).

## Authentication

All three services use optional `Authorization: Bearer <token>`.

- The **vm-agent** reads its token from a file on the VM
  (`TOKEN_FILE` in `agent.py`, default `C:\Users\Public\agent_token.txt`).
- The **bridge** and **designer** read from env vars:
  `WALLAC_BRIDGE_TOKEN`, `WALLAC_DESIGNER_TOKEN`.

If unset, auth is disabled and the service logs a warning. **No token is ever
stored in this repository.** For the full policy, see
[`auth-secrets-policy.md`](auth-secrets-policy.md).

## bridge API — `:8423`

| Method & path | Purpose |
|---|---|
| `GET /health` | bridge liveness + worker status + current job |
| `POST /jobs` | submit a job for execution (idempotent: duplicate spec → `409`) |
| `GET /jobs` | list all jobs |
| `GET /jobs/{job_id}` | job status, events, artifacts, live wells |
| `POST /jobs/{job_id}/abort` | abort a running job |

## designer API — `:8422`

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /config` | client-side URLs (bridge, eLabFTW, vm-agent) for auto-fill |
| `GET /run-builder` | Run Builder single-page app |
| `GET /elabftw/events` | proxy for eLabFTW calendar (self-signed cert workaround) |
| `POST/GET /api/{methods\|layouts\|analyses\|jobs}` | create / list drafts |
| `GET/PATCH /api/{...}/{item_id}` | read / update a draft |
| `POST /api/{...}/{item_id}/finalize` | canonicalize + hash + attach JSON |
| `POST /api/{...}/{item_id}/clone` | clone a signed object to a new draft |

For the draft/signed lifecycle and canonical JSON schemas, see
[`elabftw-object-model.md`](elabftw-object-model.md).
