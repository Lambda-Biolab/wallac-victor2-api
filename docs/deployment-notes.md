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
