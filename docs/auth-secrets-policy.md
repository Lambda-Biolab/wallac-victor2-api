# Auth & secrets policy — Wallac bridge

- **Status:** policy
- **Date:** 2026-06-25
- **Source:** issue #6, `docs/wallac-plate-reader-integration.md`,
  `docs/automation-integrations.md`

This document defines the service identity, secrets handling, access
controls, and network assumptions for the Wallac bridge.

## Service identity

The Wallac bridge uses a **dedicated eLabFTW API key** — never a shared human
admin key.  The key supports two service roles:

**Designer (Run Builder):** item CRUD in Wallac resource categories, upload
canonical JSON attachments, patch metadata extra_fields, read events.

**Executor (bridge):** download canonical JSON attachments for hash-verified
spec retrieval, create and patch experiments, upload experiment files (raw
results JSON, analyzed CSV, HTML body with heatmap).

The key does **not** have admin privileges, user management, or system config
access.

### Creating the service key

1. Create a dedicated eLabFTW user (e.g., `wallac-bridge`).
2. Generate an API key for that user (User Panel → API Keys → Create).
3. Store the key in the runtime environment (see below).
4. Document the key ID and creation date in the ops log.

## Secrets handling

**Secrets must be stored only in runtime environment variables.**  They must
not be committed to the repository; tracked templates contain placeholder
values and must never hold real credentials.

| Variable | Purpose | Required |
|---|---|---|
| `WALLAC_ELABFTW_API_KEY` | eLabFTW service API key (write-back only) | **yes** |
| `WALLAC_ELABFTW_URL` | eLabFTW base URL | no (default: `https://localhost:3148`) |
| `WALLAC_ELABFTW_VERIFY_TLS` | Verify eLabFTW TLS certificate (0/false disables) | no (default: `1`/true) |
| `WALLAC_VM_AGENT_URL` | vm-agent REST API URL | no (default: `http://192.168.122.203:8420`) |
| `WALLAC_VM_AGENT_TOKEN` | vm-agent bearer token | no (if unset, no auth) |
| `WALLAC_BRIDGE_TOKEN` | Bridge HTTP API bearer token | no (if unset, bridge is open on LAN) |
| `WALLAC_BRIDGE_URL` | Run Builder bridge URL for auto-config | no (needed for Run Builder /config) |
| `WALLAC_DESIGNER_TOKEN` | Designer HTTP API bearer token | no (if unset, designer is open on LAN) |
| `WALLAC_DRY_RUN` | Validate requests without instrument execution | no (default: `0`) |
| `WALLAC_CORS_ORIGINS` | Comma-separated CORS allowlist for bridge API | no (default: none) |
| `WALLAC_REQUIRE_AUTH` | If set to 1/true, services refuse to start with empty tokens | no (default: `0`) |

### Key storage

- Secrets are stored in the runtime environment (e.g., systemd environment
  file, Docker env, or a `.env` file that is gitignored).
- The `.gitignore` already excludes `*.token`, `*_key`, `*_key.*`,
  `agent_token.txt`, and `*.pem`.

### Key revocation

1. Revoke the key in eLabFTW (Admin Panel → Sysconfig → API Keys → Revoke,
   or the service user's profile → API Keys → Revoke).
2. Generate a new key.
3. Update the runtime environment variable.
4. Restart the bridge service.
5. Verify the bridge can write results to eLabFTW experiments.

### Audit trail

- eLabFTW logs all API key usage (who, when, what endpoint).
- The bridge logs its own actions (submit, progress, write-back, abort) with
  timestamps and job IDs.

## Bridge and Designer access controls

The bridge (`POST /jobs`, `POST /jobs/{id}/abort`) and the designer
(Run Builder drafts, finalize, `/config`) accept a bearer token from
`WALLAC_BRIDGE_TOKEN` / `WALLAC_DESIGNER_TOKEN`. When the token is
unset, the service runs open on the network.
- The `/config` and `/elabftw/events` endpoints on the designer service
  are behind the same bearer-token check; `/config` no longer returns the
  internal `vm_agent_url`.
- Both bridge and designer use plain HTTP; there is no built-in TLS. A
  reverse proxy (nginx, Caddy, Traefik) is required to expose them
  beyond a trusted LAN.

### Network assumptions

The bridge and designer services are designed for deployment on a
trusted LAN or Tailscale network:

- **Lab LAN** — services run on the lab network, accessible only to
  lab operators.
- **Tailscale** — services reachable over Tailscale, with network-layer
  access control via encrypted WireGuard tunnels.

The bridge and designer should **not** be exposed to the public
internet without a reverse proxy that enforces authentication and TLS.

### Strict-auth mode

For deployments where any open-on-LAN exposure is unacceptable, set
`WALLAC_REQUIRE_AUTH=1`. With this flag set, the bridge and designer
services refuse to start if their respective tokens are unset.

### Run Builder token sharing

The Run Builder UI accepts a single token field in its settings and uses it
for both designer and bridge API requests. When both services enforce
authentication, `WALLAC_DESIGNER_TOKEN` and `WALLAC_BRIDGE_TOKEN` **must be
set to the same value**. API clients may use separate tokens.

### Browser never receives secrets

The browser receives only operator-visible data:

- Job state and progress via the bridge GET /jobs JSON API
- Run Builder HTML/CSS/JS (static files from the designer service)
- Draft spec data (via designer CRUD API)

The browser **never** receives:

- The eLabFTW service API key
- The vm-agent token
- Any authentication token beyond the `Authorization` header
- Any internal bridge configuration

## Implementation

- Config: `bridge/config.py` — `BridgeConfig.from_env()`
- Bridge HTTP API auth: bearer token in `WALLAC_BRIDGE_TOKEN` env var
