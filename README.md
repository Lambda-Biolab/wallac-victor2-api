# wallac-victor2-api

A **REST/JSON API** for the PerkinElmer **Wallac Victor2 (1420)** multimode
microplate reader — start assays, stream live status, and pull results over
HTTP, driven from Linux.

The original instrument software exposes only a 2005-vintage Windows GUI with
no automatable interface. This project runs the **unmodified OEM software**
inside a Windows 7 VM and wraps its COM automation server behind a clean HTTP
API, so a used Victor2 / 1420 can drop straight into a Linux/robotics
screening pipeline (TR-fluorescence, prompt fluorescence, absorbance/
photometry, luminescence, fluorescence polarization).

> **Scope:** this repository contains only the API microservice and its
> documentation. It does **not** contain or redistribute any PerkinElmer /
> Wallac software, firmware, or installation media — you must supply a
> legitimately licensed OEM installation in the VM.

## Architecture

```
Linux orchestrator
   │  HTTP/JSON + SSE   (libvirt NAT, optional bearer token)
   ▼
Windows 7 VM ── vm-agent/agent.py   (Python 3.8 + comtypes, runs as the console user)
   │  COM automation   (ProgID Wallac1420.Server)
   ▼
OEM MlrServ / MlrMgr ──► Victor2 / 1420 reader
```

- **`vm-agent/agent.py`** runs *inside* the VM as the interactive desktop user
  (COM/OLE automation only works there). It drives `MlrServ`'s COM server and
  serves REST/JSON + Server-Sent Events on the VM's libvirt NAT interface.
- COM is apartment-threaded, so a single dedicated STA worker owns the COM
  object; HTTP handler threads marshal calls to it. A separate thread holds its
  own COM connection for ~1 Hz real-time monitoring, so a long operation never
  freezes the status stream.
- Supporting pieces:
  - **`launch_as_user.py`** — start the OEM GUI / agent as the interactive
    console user from a SYSTEM context (`CreateProcessAsUser`, no password).
  - **`lid_watcher.py`** — auto-dismiss a faulty lid-interlock dialog so it
    doesn't stall measurements (every action is logged for auditability).
  - **`start-stack.bat`** — Startup-folder autostart that brings the whole
    stack up on logon.
  - **`probe.py`, `dump_methods.py`, `dump_protocols.py`, `dump_tlb.py`** —
    COM-introspection diagnostics used when extending the API.

## API

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness + instrument connection state |
| `GET /status` | latest cached instrument snapshot (from the monitor) |
| `GET /monitor` | **SSE** real-time state stream (~1 Hz) |
| `GET /instrument` | model, serial, technologies, temperature |
| `GET /protocols` | assay protocols from the instrument DB (`?refresh=1` to reload) |
| `POST /runs` | start an assay (`{"protocol_id": …}`; `"dry_run": true` to validate) |
| `GET /runs` / `GET /runs/{id}` | run list / single run state |
| `GET /runs/{id}/results` | live per-well counts for the active run |
| `GET /runs/{id}/export` | live results as CSV |
| `POST /runs/{id}/abort` | cancel a run (guarded: only ≥60 s in) |
| `GET /jobs` / `GET /jobs/{id}` | completed assays |
| `GET /jobs/{id}/results` | per-well results (JSON, incl. computed OD) |
| `GET /jobs/{id}/export` | CSV — `?format=long\|grid`, `?value=raw\|od` |
| `POST /admin/reconnect` | re-establish the COM link |

**Auth:** optional `Authorization: Bearer <token>`. The token is read at
startup from a file on the VM (`TOKEN_FILE` in `agent.py`); if that file is
absent, auth is disabled and the agent logs a warning (the libvirt NAT is
host-only). **No token is ever stored in this repository.**

Full request/response details: [`docs/api-reference.md`](docs/api-reference.md).

## Install & use

**Prerequisites**

- A Linux/QEMU host running a **Windows 7 SP1** guest on the libvirt NAT.
- A licensed **Wallac 1420 / Victor2** OEM installation inside the guest, with
  the reader connected and recognized by the OEM stack (i.e. `MlrMgr` shows the
  instrument online).
- In the guest: **Python 3.8 (32-bit)** + `comtypes` (the OEM COM server and
  DAO are 32-bit).

**Deploy the agent (in the VM)**

1. Copy `vm-agent/` into the guest (e.g. `C:\install\`).
2. (Optional auth) write a secret token to the path named by `TOKEN_FILE` in
   `agent.py` (default `C:\Users\Public\agent_token.txt`).
3. Enable autologon for the desktop user and put `start-stack.bat` in that
   user's Startup folder. On boot it launches the OEM GUI, the lid watcher, and
   the agent.

On boot the agent listens on the guest's libvirt NAT address, port **8420**.

**Use** — from the Linux host (replace the IP with your guest's NAT address;
`H` is the optional bearer header):

```bash
H='Authorization: Bearer <token>'
VM=192.168.122.203:8420        # your Windows 7 guest on the libvirt NAT

curl -H "$H" "http://$VM/health"
curl -H "$H" "http://$VM/protocols"

# start an assay, then poll it:
curl -H "$H" -H 'Content-Type: application/json' \
     -d '{"protocol_id":1000003}' "http://$VM/runs"
curl -H "$H" "http://$VM/runs/<run_id>"

curl -N -H "$H" "http://$VM/monitor"     # live state (SSE)

# results as an 8x12 grid of computed absorbance:
curl -H "$H" "http://$VM/jobs/<id>/export?format=grid&value=od"
```

## Development

Quality gates apply to the maintained agent stack
(`vm-agent/{agent,lid_watcher,launch_as_user}.py`). Tools run via `uv`, so no
global installs are needed:

```bash
make validate    # ruff lint + format-check + complexity (<=15) + pytest
make format      # auto-fix lint + format
make test        # unit tests only
make setup_dev   # install the pre-commit hooks
```

The unit tests cover the pure data-shaping helpers (background parsing, OD
computation, plate-grid CSV) and run on any OS; the COM/HTTP paths require
Windows, `comtypes`, and a live instrument.

## License

Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE). This project controls, but does not include or
redistribute, any PerkinElmer / Wallac instrument software, firmware, or
installation media; supply your own legitimately licensed OEM installation.
