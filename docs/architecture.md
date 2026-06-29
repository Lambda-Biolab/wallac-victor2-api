# Architecture

System overview for the Wallac Victor2 (1420) API and eLabFTW bridge. For the
per-endpoint contract, see [`api-reference.md`](api-reference.md) and
[`bridge-api.md`](bridge-api.md); for the direct-submit design decision, see
[`architecture-direct-submit.md`](architecture-direct-submit.md).

## Diagram

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

## vm-agent — instrument microservice (Windows VM)

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

## bridge — eLabFTW integration (Linux host)

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

## Key design decisions

(See [`architecture-direct-submit.md`](architecture-direct-submit.md) for the
rationale and rejected alternatives.)

- eLabFTW is the **archive**, not the job queue. The Run Builder submits jobs
  directly to the bridge via HTTP POST — no polling.
- Draft objects (Method/Layout/Analysis/Job) are mutable; signed objects
  reject mutation (routed to clone/version). Canonical JSON is
  deterministically serialized and SHA-256 hashed.
- The browser never receives the eLabFTW API key or vm-agent token — all
  eLabFTW interaction happens server-side.
- Result write-back is resilient: if eLabFTW is unreachable, results are
  spooled to disk (`bridge/spool.py`) and retried.
