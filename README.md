# wallac-victor2-api

[![License](https://img.shields.io/badge/license-Apache--2.0-58f4c2.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8-58f4c2.svg)](https://www.python.org/)
[![CI](https://github.com/Lambda-Biolab/wallac-victor2-api/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Lambda-Biolab/wallac-victor2-api/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Lambda-Biolab/wallac-victor2-api/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/Lambda-Biolab/wallac-victor2-api/actions/workflows/codeql.yml)

A **REST/JSON API** for the PerkinElmer **Wallac Victor2 (1420)** multimode
microplate reader — start assays, stream live status, and pull results over
HTTP, driven from Linux.

The original instrument software exposes only a 2005-vintage Windows GUI with
no automatable interface. This project runs the **unmodified OEM software**
inside a Windows 7 VM and wraps its COM automation server behind a clean HTTP
API, so a used Victor2 / 1420 can drop straight into a Linux/robotics
screening pipeline (TR-fluorescence, prompt fluorescence, absorbance/
photometry, luminescence, fluorescence polarization).

A second stack — the **bridge** — runs on the Linux host and integrates the
instrument with **eLabFTW** (electronic lab notebook): a Run Builder UI for
protocol authoring, a direct-submit job API, result write-back, and a live
dashboard.

> **Scope:** this repository contains the instrument API microservice
> (`vm-agent/`), the eLabFTW bridge (`bridge/`), and documentation. It does
> **not** contain or redistribute any PerkinElmer / Wallac software, firmware,
> or installation media — you must supply a legitimately licensed OEM
> installation in the VM.

## Demo

![Wallac Victor2 API and run-builder UI demonstration](gif/wallac-api.gif)

The HTTP API driving the Victor2 / 1420 from Linux, with the run-builder UI
assembling a protocol and streaming live results back over SSE.

## Architecture

```
                         Linux host
  ┌─────────────────────────────────────────────────────────────┐
  │                                                             │
  │  Run Builder (browser)                                      │
  │    │  HTTP                                                  │
  │    ▼                                                        │
  │  designer_app  :8422   ──►  eLabFTW  (drafts, signed specs)  │
  │    │  POST /jobs                                           │
  │    ▼                                                        │
  │  bridge_app    :8423   ──►  eLabFTW  (experiment + results) │
  │    │  HTTP/JSON + SSE                                       │
  │    ▼                                                        │
  └────┬─────────────────────────────────────────────────────────┘
       │  libvirt NAT (optional bearer token)
       ▼
  Windows 7 VM ── vm-agent/agent.py   (Python 3.8 + comtypes, console user)
       │  COM automation   (ProgID Wallac1420.Server)
       ▼
  OEM MlrServ / MlrMgr ──► Victor2 / 1420 reader
```

### vm-agent — instrument microservice (Windows VM)

**`vm-agent/agent.py`** runs *inside* the VM as the interactive desktop user
(COM/OLE automation only works there). It drives `MlrServ`'s COM server and
serves REST/JSON + Server-Sent Events on the VM's libvirt NAT interface.

COM is apartment-threaded, so a single dedicated STA worker owns the COM
object; HTTP handler threads marshal calls to it. A separate thread holds its
own COM connection for ~1 Hz real-time monitoring, so a long operation never
freezes the status stream.

Supporting pieces:

- **`launch_as_user.py`** — start the OEM GUI / agent as the interactive
  console user from a SYSTEM context (`CreateProcessAsUser`, no password).
- **`lid_watcher.py`** — auto-dismiss a faulty lid-interlock dialog so it
  doesn't stall measurements (every action is logged for auditability).
- **`start-stack.bat`** — Startup-folder autostart that brings the whole
  stack up on logon.
- **`probe.py`, `dump_methods.py`, `dump_protocols.py`, `dump_tlb.py`** —
  COM-introspection diagnostics used when extending the API.

### bridge — eLabFTW integration (Linux host)

The bridge sits between the user and the vm-agent. It accepts job submissions
(via HTTP from the Run Builder), executes them against the vm-agent, and
writes results back to eLabFTW as experiment records. Three FastAPI apps:

- **`bridge/bridge_app.py`** (`:8423`) — direct-submit job API. Accepts
  `POST /jobs`, executes on a background worker thread, writes results to
  eLabFTW. Replaces the old eLabFTW-polling daemon (`main.py`).
- **`bridge/designer_app.py`** (`:8422`) — Run Builder backend. CRUD for
  Method, Plate Layout, Analysis Plan, and Automation Job draft objects;
  finalize (canonicalize + SHA-256 hash); clone signed objects. Serves the
  Run Builder single-page app at `GET /run-builder`.
- **`bridge/dashboard.py`** (`:8421`) — live status dashboard, served by
  `main.py`.

Key design decisions (see [`docs/architecture-direct-submit.md`](docs/architecture-direct-submit.md)):

- eLabFTW is the **archive**, not the job queue. The Run Builder submits jobs
  directly to the bridge via HTTP POST — no polling.
- Draft objects (Method/Layout/Analysis/Job) are mutable; signed objects
  reject mutation (routed to clone/version). Canonical JSON is
  deterministically serialized and SHA-256 hashed.
- The browser never receives the eLabFTW API key or vm-agent token — all
  eLabFTW interaction happens server-side.
- Result write-back is resilient: if eLabFTW is unreachable, results are
  spooled to disk (`bridge/spool.py`) and retried.

## API

### vm-agent API — `:8420`

| Method & path | Purpose |
|---|---|
| `POST /measure` | **one call: run a protocol by name, wait, return the OD table** |
| `GET /docs` | self-describing catalog of every endpoint |
| `GET /health` | liveness + `ready` flag + instrument connection state |
| `GET /status` | latest cached instrument snapshot (from the monitor) |
| `GET /monitor` | **SSE** real-time state stream (~1 Hz) |
| `GET /instrument` | model, serial, technologies, temperature |
| `GET /protocols` | assay protocols (`?q=` to search, `?refresh=1` to reload) |
| `GET /protocols/{name\|id}` | resolve one protocol by **name** or id |
| `POST /runs` | start an assay (`{"protocol": "<name\|id>"}`; `"dry_run": true`) |
| `GET /runs` / `GET /runs/{id}` | run list / single run state |
| `GET /runs/{id}/results` | results — **live while running, persisted once measured** |
| `GET /runs/{id}/export` | results as CSV (`?shape=grid`, `?value=od\|raw`) |
| `POST /runs/{id}/abort` | cancel a run (guarded: only ≥60 s in) |
| `DELETE /runs/{id}` | drop a finished/failed/stuck run record (`?force=1`) |
| `GET /jobs` / `GET /jobs/{id}` | completed assays |
| `GET /jobs/{id}/results` | per-well results (JSON, deduped; `?dedup=0` for raw rows) |
| `GET /jobs/{id}/export` | CSV — `?format=long\|grid`, `?value=raw\|od` |
| `POST /admin/reconnect` | re-establish the COM link |

The friendly entrypoint is **`POST /measure`**: give it a protocol **name**, it
resolves the id, starts the run, waits for the plate read, and returns the
deduped per-well OD table (optionally an 8×12 grid). Errors are actionable —
e.g. `409 instrument_not_ready` with a `hint` telling you to close the lid —
rather than raw COM tracebacks.

### bridge API — `:8423`

| Method & path | Purpose |
|---|---|
| `GET /health` | bridge liveness + worker status + current job |
| `POST /jobs` | submit a job for execution (idempotent: duplicate spec → `409`) |
| `GET /jobs` | list all jobs |
| `GET /jobs/{job_id}` | job status, events, artifacts, live wells |
| `POST /jobs/{job_id}/abort` | abort a running job |

### designer API — `:8422`

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

**Auth:** all three services use optional `Authorization: Bearer <token>`.
The vm-agent reads its token from a file on the VM (`TOKEN_FILE` in
`agent.py`); the bridge and designer read from env vars (`WALLAC_BRIDGE_TOKEN`,
`WALLAC_DESIGNER_TOKEN`). If unset, auth is disabled and the service logs a
warning. **No token is ever stored in this repository.**

Full request/response details: [`docs/api-reference.md`](docs/api-reference.md).

## Install & use

**Prerequisites**

- A Linux/QEMU host running a **Windows 7 SP1** guest on the libvirt NAT.
- A licensed **Wallac 1420 / Victor2** OEM installation inside the guest, with
  the reader connected and recognized by the OEM stack (i.e. `MlrMgr` shows the
  instrument online).
- In the guest: **Python 3.8 (32-bit)** + `comtypes` (the OEM COM server and
  DAO are 32-bit).
- On the host: **Python 3.11+** + `uv` (for tooling) and the bridge
  dependencies (`fastapi`, `uvicorn`, `pydantic`, `pynacl`).

**Deploy the vm-agent (in the VM)**

1. Copy `vm-agent/` into the guest (e.g. `C:\install\`).
2. (Optional auth) write a secret token to the path named by `TOKEN_FILE` in
   `agent.py` (default `C:\Users\Public\agent_token.txt`).
3. Enable autologon for the desktop user and put `start-stack.bat` in that
   user's Startup folder. On boot it launches the OEM GUI, the lid watcher, and
   the agent.

On boot the agent listens on the guest's libvirt NAT address, port **8420**.

**Deploy the bridge (on the Linux host)**

1. Copy `deploy/bridge.env.example` to `/etc/wallac-bridge/bridge.env` and
   fill in the eLabFTW URL + API key, vm-agent URL + token, and optional
   dashboard/designer/bridge tokens.
2. Install the systemd services:

   ```bash
   sudo cp deploy/wallac-bridge.service  /etc/systemd/system/
   sudo cp deploy/wallac-designer.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now wallac-bridge wallac-designer
   ```

The bridge listens on `:8423`, the designer/Run Builder on `:8422`.

**Use** — from the Linux host (replace the IP with your guest's NAT address;
`H` is the optional bearer header):

```bash
H='Authorization: Bearer <token>'
VM=192.168.122.203:8420        # your Windows 7 guest on the libvirt NAT

curl -H "$H" "http://$VM/health"
curl -H "$H" "http://$VM/protocols?q=absorb"          # search protocols by name

# the easy path — run a protocol by NAME, wait, get the OD table back:
curl -H "$H" -H 'Content-Type: application/json' \
     -d '{"protocol":"Absorbance @ 600"}' "http://$VM/measure"

# ...or drive it yourself: start without waiting, poll, then fetch results:
curl -H "$H" -H 'Content-Type: application/json' \
     -d '{"protocol":"Absorbance @ 600","wait":false}' "http://$VM/measure"
curl -H "$H" "http://$VM/runs/<run_id>"
curl -H "$H" "http://$VM/runs/<run_id>/results?shape=grid&value=od"

curl -N -H "$H" "http://$VM/monitor"     # live state (SSE)

# any past run's results as an 8x12 grid of computed absorbance:
curl -H "$H" "http://$VM/jobs/<id>/export?format=grid&value=od"
```

## Operations & internals

The operational runbook (start / verify / restart the `win7-wallac` VM, the
ARCnet / VFIO passthrough setup) and the bench gotchas live in the internal
sister repo **`wallac-victor2-linux`** at `host-config/VM-OPERATIONS.md` — they
are environment-specific, so they are not duplicated in this public package.

> **Deployment gotcha that does belong here:** the Wallac OEM installer nests its
> files under `C:\Program Files\Wallac\Wallac1420\` (note the extra `Wallac\`);
> the flat `C:\Program Files\Wallac1420\` directory exists but is **empty**. The
> path constants in `vm-agent/` use the nested form — if your OEM install
> differs, verify inside the VM with `where /R "C:\Program Files" MlrMgr.exe` and
> adjust. (The flat form was a real bug here — it made `start-stack.bat` fail with
> *"Windows cannot find 'MlrMgr.exe'"*.)

## Development

Quality gates apply to both maintained stacks — the vm-agent
(`vm-agent/{agent,lid_watcher,launch_as_user}.py`) and the bridge
(`bridge/*.py`). Tools run via `uv`, so no global installs are needed:

```bash
make validate    # ruff lint + format-check + complexity (<=15) + pytest
make format      # auto-fix lint + format
make test        # unit tests only
make setup_dev   # install the pre-commit hooks
```

The unit tests (342 tests) cover the pure data-shaping helpers in both stacks:
vm-agent background parsing, OD computation, plate-grid CSV; bridge intake,
lifecycle, writeback, validation, analysis, jobs, designer, execution,
canonical hashing, and generated protocols. They run on any OS; the COM/HTTP
paths require Windows, `comtypes`, and a live instrument.

## Documentation

- [API reference](docs/api-reference.md) — full vm-agent request/response details
- [Direct-submit architecture](docs/architecture-direct-submit.md) — bridge design
  decision (eLabFTW as archive, not job queue)
- [eLabFTW object model](docs/elabftw-object-model.md) — resource categories,
  draft/signed lifecycle, canonical JSON schemas
- [Auth & secrets policy](docs/auth-secrets-policy.md) — token handling, what is
  and isn't stored
- [Abort recovery](docs/abort-recovery.md) — job abort flow and spool recovery
- [Stage 7 hardware E2E test plan](docs/stage7-hardware-e2e-test-plan.md) —
  hardware validation protocol

## License

Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE). This project controls, but does not include or
redistribute, any PerkinElmer / Wallac instrument software, firmware, or
installation media; supply your own legitimately licensed OEM installation.
