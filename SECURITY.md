# Security

> **The Wallac Victor2 bridge is currently designed for deployment on a
> trusted local area network or a Tailscale-only network. It must not be
> exposed to the public internet without additional hardening (see
> [Hardening for broader exposure](#hardening-for-broader-exposure) below).**

## Threat model

The bridge, designer, dashboard, and vm-agent services are written for a
lab environment where:

- All three HTTP services (bridge, designer, dashboard) bind to
  `0.0.0.0` and accept bearer-token authentication. If the corresponding
  `WALLAC_*_TOKEN` environment variable is unset, the service runs
  **open** on the network and relies entirely on the network boundary
  for access control.
- The vm-agent runs inside a Windows 7 libvirt VM behind a host-only NAT
  on `192.168.122.x`. It is expected to be reached only by the bridge
  over an SSH tunnel; the NAT provides the access-control boundary.
- All services communicate over plain HTTP. There is no built-in TLS.
- The `/config` endpoint on the designer service historically returned
  internal URLs (eLabFTW, bridge, vm-agent). It is now behind
  authentication and omits the internal `vm_agent_url`.
- Cross-origin requests are no longer allowed by default; configure
  `WALLAC_CORS_ORIGINS` explicitly to enable a browser frontend.
- eLabFTW connections use TLS verification by default; self-signed
  certificates require an explicit opt-out (`WALLAC_ELABFTW_VERIFY_TLS=0`).

This is appropriate for a lab LAN, a Tailscale network, or a similarly
trusted boundary. It is **not** appropriate for the public internet.

For the per-endpoint authentication contract, see
[`docs/auth-secrets-policy.md`](docs/auth-secrets-policy.md).

## Hardening for broader exposure

To deploy the bridge beyond a trusted LAN (for example, behind a public
reverse proxy or a public Tailscale node):

1. **Set every auth token.** `WALLAC_BRIDGE_TOKEN`, `WALLAC_DESIGNER_TOKEN`,
   and `WALLAC_DASHBOARD_TOKEN` must be set to long, random values.
   Alternatively, set `WALLAC_REQUIRE_AUTH=1` to hard-fail at startup if
   any are unset.
2. **Front every service with a TLS-terminating reverse proxy** (nginx,
   Caddy, Traefik, etc.). The services themselves speak plain HTTP and
   do not implement TLS.
3. **Tighten the CORS allowlist.** `WALLAC_CORS_ORIGINS` should list
   only the exact origins that need browser access (e.g.
   `https://run-builder.example.com`). The previous default of `*` has
   been removed.
4. **Keep eLabFTW TLS verification on.** If your eLabFTW uses a
   self-signed certificate, install it in the system trust store
   instead of disabling verification globally.
5. **Bind to a specific interface, not `0.0.0.0`.** Set
   `WALLAC_DASHBOARD_HOST` to the reverse proxy's loopback address if
   appropriate. (Bridge and designer bind via systemd unit; update the
   unit to use `--host 127.0.0.1` and let the proxy reach them.)
6. **Review the firewall.** Confirm that ports `8420-8423` are not
   reachable from outside the trusted boundary, even with the reverse
   proxy in place.

## Reporting a vulnerability

This is a research/lab project without a paid security program. If you
find a vulnerability, open a private security advisory on GitHub or
contact the maintainer directly. Please do not file public issues for
unpatched security bugs.

## Related documentation

- [`docs/auth-secrets-policy.md`](docs/auth-secrets-policy.md) — token
  configuration, dashboard network assumptions, secrets-in-browser
  guarantees.
- [`docs/deployment-notes.md`](docs/deployment-notes.md) — production
  deployment topology and SSH-tunneling for the vm-agent.
- [`docs/architecture.md`](docs/architecture.md) — component
  responsibilities and trust boundaries.
