# Auth & secrets policy — Wallac bridge

- **Status:** policy
- **Date:** 2026-06-25
- **Source:** issue #6, `docs/wallac-plate-reader-integration.md`,
  `docs/automation-integrations.md`

This document defines the service identity, secrets handling, dashboard
access controls, and network assumptions for the Wallac bridge.

## Service identity

The Wallac bridge uses a **dedicated eLabFTW API key** — never a shared human
admin key.  The key has minimum permissions to:

- Read Automation Job resources (items in the Automation Job category)
- Read uploads (signature archives)
- Write metadata fields (state, progress, results, errors)
- Upload result artifacts
- Post comments (event log)

The key does **not** have admin privileges, user management, or system config
access.  It is scoped to the Automation Job category and linked experiments.

### Creating the service key

1. Create a dedicated eLabFTW user (e.g., `wallac-bridge`).
2. Generate an API key for that user (User Panel → API Keys → Create).
3. Store the key in the runtime environment (see below).
4. Document the key ID and creation date in the ops log.

## Secrets handling

**All secrets live in runtime environment variables.**  No secrets are
committed to the repository, stored in config files, or written to logs.

| Variable | Purpose | Required |
|---|---|---|
| `WALLAC_ELABFTW_API_KEY` | eLabFTW service API key | **yes** |
| `WALLAC_ELABFTW_URL` | eLabFTW base URL | no (default: `https://localhost:3148`) |
| `WALLAC_ELABFTW_CATEGORY` | Automation Job category ID | no (default: 9) |
| `WALLAC_VM_AGENT_URL` | vm-agent REST API URL | no (default: `http://192.168.122.203:8420`) |
| `WALLAC_VM_AGENT_TOKEN` | vm-agent bearer token | no (if unset, no auth) |
| `WALLAC_DASHBOARD_TOKEN` | Dashboard session token | no (if unset, dashboard is open on LAN) |
| `WALLAC_DASHBOARD_HOST` | Dashboard bind address | no (default: `0.0.0.0`) |
| `WALLAC_DASHBOARD_PORT` | Dashboard port | no (default: 8421) |
| `WALLAC_BRIDGE_IDENTITY` | Bridge instance name (for "Claimed by") | no (default: `wallac-bridge`) |
| `WALLAC_DEVICE_IDENTITY` | Device identity string | no (default: `victor2-unknown`) |

### Key storage

- Secrets are stored in the runtime environment (e.g., systemd environment
  file, Docker env, or a `.env` file that is gitignored).
- The `.gitignore` already excludes `*.token`, `*_key`, `*_key.*`,
  `agent_token.txt`, and `*.pem`.
- `BridgeConfig.redacted()` masks all secret values for safe logging.

### Key revocation

1. Revoke the key in eLabFTW (Admin Panel → Sysconfig → API Keys → Revoke,
   or the service user's profile → API Keys → Revoke).
2. Generate a new key.
3. Update the runtime environment variable.
4. Restart the bridge service.
5. Verify the bridge can poll eLabFTW and claim jobs.

### Audit trail

- eLabFTW logs all API key usage (who, when, what endpoint).
- The bridge logs its own actions (claim, progress, write-back, abort) with
  timestamps and job IDs.
- The dashboard's event log (eLabFTW comments) provides a durable audit
  trail of job lifecycle events.

## Dashboard access controls

### Session token

If `WALLAC_DASHBOARD_TOKEN` is set, all dashboard endpoints require:

```
Authorization: Bearer <token>
```

Without the token (or with a wrong token), all endpoints return `401
{"error": "unauthorized"}`.  This includes the HTML page, JSON API, SSE
stream, and artifact downloads.

If the token is unset, the dashboard is open to anyone on the network.
This is acceptable on a host-only libvirt NAT or a Tailscale-only network,
but **should not** be used on a public network.

### Network assumptions

The dashboard is designed for:

- **Lab LAN** — the bridge runs on the lab network, accessible only to
  lab operators.
- **Tailscale** — the bridge is reachable over Tailscale, which provides
  encrypted WireGuard tunnels and access control at the network layer.

The dashboard should **not** be exposed to the public internet without a
reverse proxy that enforces authentication and TLS.

### Browser never receives secrets

The browser receives only operator-visible data:

- Job state, progress, and results (via JSON API and SSE)
- Dashboard HTML/CSS/JS (static files)
- Result artifacts (CSV/grid downloads)

The browser **never** receives:

- The eLabFTW service API key
- The vm-agent token
- The dashboard session token (beyond the Authorization header)
- Any internal bridge configuration

This is verified by `bridge/secrets_check.py`, which scans rendered
dashboard HTML/JS for secret values and common secret patterns.  The tests
in `tests/test_bridge_hardening.py` enforce this at CI time.

## Implementation

- Config: `bridge/config.py` — `BridgeConfig.from_env()`
- Secrets scan: `bridge/secrets_check.py` — `scan_for_secrets()`
- Dashboard auth: `bridge/dashboard.py` — `DashboardHandler._authorized()`
- Tests: `tests/test_bridge_hardening.py`
