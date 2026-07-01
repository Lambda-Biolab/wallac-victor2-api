# Deployment notes

Environment-specific operational runbook details (start / verify / restart the
`win7-wallac` VM, ARCnet / VFIO passthrough setup) and bench gotchas live in
the internal sister repo **`wallac-victor2-linux`** at
`host-config/VM-OPERATIONS.md` — they are environment-specific and are not
duplicated in this public package.

## OEM install path gotcha

The Wallac OEM installer nests its files under
`C:\Program Files\Wallac\Wallac1420\` (note the extra `Wallac\`); the flat
`C:\Program Files\Wallac1420\` directory exists but is **empty**. The path
constants in `vm-agent/` use the nested form — if your OEM install differs,
verify inside the VM with:

```
where /R "C:\Program Files" MlrMgr.exe
```

and adjust. (The flat form was a real bug here — it made `start-stack.bat`
fail with *"Windows cannot find 'MlrMgr.exe'".*)

## SSH tunnel networking (Docker gateway → vm-agent)

The lab-copilot-gateway runs in a Docker container on the Linux host and
reaches the vm-agent on the Windows VM (192.168.122.203) via an SSH tunnel:

```
ssh -f -N -L 172.17.0.1:8420:192.168.122.203:8420 antonio@lambdabiolab-computer
```

The tunnel binds to the Docker bridge gateway IP (`172.17.0.1`) on port 8420
(not `localhost` — the gateway container resolves `localhost` to its own
container namespace, not the host). The gateway's env var points to the tunnel:

```
LAB_COPILOT_WALLAC_BASE_URL=http://172.17.0.1:8420
```

Docker compose must add `extra_hosts` on the gateway service so
`host.docker.internal` also resolves to the host:

```yaml
services:
  gateway:
    extra_hosts: ["host.docker.internal:host-gateway"]
```

This pattern keeps the vm-agent behind the libvirt NAT with no direct Docker
network exposure. The SSH tunnel is the only ingress path.
