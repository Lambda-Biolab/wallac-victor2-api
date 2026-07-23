# Stage 7: Hardware E2E Acceptance Test Plan

**Date:** 2026-06-27
**Plan reference:** `docs/plans/wallac-protocol-authoring.md` Stage 7
**Status:** Ready for execution (all prior stages implemented and merged)

## Purpose

Validate the `generated_protocol` execution path on the live Wallac Victor2
instrument. The `existing_protocol` path was already validated during earlier
e2e testing (96-well reads at 405nm and 600nm with real OD data). This plan
tests the full generated-protocol authoring pipeline: signed bundle → MDB
protocol generation → execution → analysis → write-back.

## Prerequisites

1. **vm-agent running** on `win7-wallac` (192.168.122.203:8420)
2. **Bridge daemon running** on `lambdabiolab-computer`
3. **eLabFTW** accessible at `https://localhost:3148`
4. **Designer app** running (optional, for Run Builder UI)
5. **Feature flag enabled:** `WALLAC_ENABLE_PROTOCOL_AUTHORING=true` on the vm-agent
6. **Protocol group exists:** `eLabFTW Generated` in the MDB (create via OEM GUI if missing)
7. **Template protocol exists:** A safe operator-installed photometry template
   (e.g., `Absorbance @ 600 (0.1s)`, AssayProtID=2000001) for the generator to copy from
8. **Plate loaded:** 96-well plate with colored dyes (for visual verification of OD differences)

## Test sequence

### Test 1: MDB endpoint connectivity

**Goal:** Verify the new `/mdb/*` endpoints work against the live MDB.

```bash
# Get protocol group ID
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/groups?name=eLabFTW%20Generated" | jq

# Get max protocol ID
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/max-protocol-id" | jq

# Query existing protocols
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT AssayProtID, ProtName FROM AssayProtocol WHERE ProtName ALIKE '\''ELAB-Run-%'\''"}' \
  "http://192.168.122.203:8420/mdb/query" | jq
```

**Pass criteria:**
- Group lookup returns a valid `group_id`
- Max protocol ID returns the current highest AssayProtID
- Query returns empty list (no existing generated protocols) or lists prior test protocols

### Test 2: Backup creation

**Goal:** Verify MDB backup works.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"stage7_test_backup.mdb"}' \
  "http://192.168.122.203:8420/mdb/backup" | jq
```

**Pass criteria:**
- Returns `{"backup_path": "C:\\Users\\Public\\mdb_backups\\stage7_test_backup.mdb", "created": true}`
- Backup file exists at the returned path (verify on the VM)

### Test 3: Generated protocol creation

**Goal:** Verify a generated MDB protocol can be created, verified, and deleted.

```bash
# Insert a test protocol
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"AssayProtID":2000099,"ProtName":"ELAB-Run-test-stage7","ProtNumber":99,"ProtVersion":1,"FactoryPreset":false,"ProtGroup":1}' \
  "http://192.168.122.203:8420/mdb/protocols" | jq

# Verify it exists
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols/2000099" | jq '.ProtName'

# Find by name
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols?name=ELAB-Run-test-stage7" | jq '.ProtName'

# Delete it
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols/2000099" | jq
```

**Pass criteria:**
- Insert returns `{"assay_prot_id": 2000099, "created": true}`
- GET by ID returns the correct `ProtName`
- Find by name returns the correct protocol
- Delete returns `{"assay_prot_id": 2000099, "deleted": true}`
- Subsequent GET returns 404

### Test 4: Full generated_protocol execution

**Goal:** Execute a generated protocol through the full bridge pipeline.

**Steps:**

1. **Create a Method** (via Run Builder or API):
   - Mode: `photometry`
   - Filter: `P610` (610nm, installed)
   - Read time: 0.1s
   - Finalize the method (attaches canonical `method.json`)

2. **Create a Plate Layout**:
   - 96-well, all wells measured
   - Finalize the layout (attaches canonical `layout.json`)

3. **Create an Analysis Plan**:
   - Blank subtraction: enabled (use row H as blank)
   - Replicate groups: rows A-G
   - Finalize the analysis (attaches canonical `analysis.json`)

4. **Sign all three objects** in eLabFTW (operator signs each one)

5. **Create an Automation Job**:
   - Execution mode: `generated_protocol`
   - Reference the signed Method, Layout, and Analysis by ID and hash
   - Finalize the job (attaches canonical `job.json`)

6. **Sign the Automation Job** in eLabFTW

7. **Submit the job** via ``POST /jobs`` on the bridge HTTP API:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "title": "Stage 7 Test 4 generated protocol",
       "execution_mode": "generated_protocol",
       "method_ref": {"object_id": <method_id>, "attachment_id": <method_att_id>, "hash": "<method_hash>"},
       "layout_ref": {"object_id": <layout_id>, "attachment_id": <layout_att_id>, "hash": "<layout_hash>"},
       "analysis_ref": {"object_id": <analysis_id>, "attachment_id": <analysis_att_id>, "hash": "<analysis_hash>"}
     }' \
     "http://lambdabiolab-computer:8423/jobs" | jq
   ```

8. **Monitor execution** via ``GET /jobs/{job_id}`` on the bridge HTTP API
   (``http://lambdabiolab-computer:8423/jobs/{job_id}``) — polls status, live
   well values, and event log.

**Pass criteria:**
- [ ] ``POST /jobs`` returns ``201 Created`` with ``job_id`` and ``status: "accepted"``
- [ ] Validation passes (signed bundle hashes verified via ``_download_ref``)
- [ ] Protocol template cloned as ``ELAB-Run-{new_id}`` (visible in executor event log as ``protocol_cloned``)
- [ ] Temporary clone cleaned up after execution (``_cleanup_cloned_protocol`` called; no ``ELAB-Run-*`` remains in MDB)
- [ ] Run starts by numeric AssayProtID (not by name)
- [ ] Run completes (state → ``measured``)
- [ ] Raw results retrieved (96 wells)
- [ ] Result completeness check passes (all expected wells present)
- [ ] Analysis runs (blank subtraction, replicate aggregation, pass/fail)
- [ ] eLabFTW experiment created/patched with rich HTML body (plate heatmap + results table)
- [ ] ``{job_id}_raw_results.json`` uploaded as experiment attachment
- [ ] ``{job_id}_analyzed.csv`` uploaded when analysis provides results
- [ ] Job state → ``completed``

### Test 5: OEM OD comparison

**Goal:** Compare vm-agent-derived OD values against OEM export.

**Steps:**
1. After Test 4 completes, export the same run's results from the OEM GUI (MlrMgr)
2. Compare the OEM OD values with the `raw_results.json` OD values

**Pass criteria:**
- [ ] OD values match within ±0.001 (floating point tolerance)
- [ ] Well ordering matches (A1-H12, row-major)
- [ ] No missing or extra wells

### Test 6: Cleanup query

**Goal:** Verify the ``POST /mdb/query`` read-only endpoint can list generated
protocols by name prefix, and that factory/user protocols are excluded.

```bash
# Query for run-clone protocols by prefix (safe read-only, no MDB writes)
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT AssayProtID, ProtName FROM AssayProtocol WHERE ProtName ALIKE '\''ELAB-Run-%'\''"}' \
  "http://192.168.122.203:8420/mdb/query" | jq
```

**Pass criteria:**
- [ ] Returns at most the protocol rows whose ``ProtName`` starts with ``ELAB-Run-``
- [ ] Factory presets are NOT listed
- [ ] User GUI protocols are NOT listed
- [ ] The query does not modify the MDB (read-only)

### Test 7: Feature flag enforcement

**Goal:** Verify generated authoring is disabled by default.

**Steps:**
1. Stop the vm-agent
2. Unset `WALLAC_ENABLE_PROTOCOL_AUTHORING` (or set to `false`)
3. Start the vm-agent
4. Try to insert a protocol:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"AssayProtID":2000098,"ProtName":"ELAB-Run-test-flag","ProtNumber":98,"ProtVersion":1,"FactoryPreset":false,"ProtGroup":1}' \
  "http://192.168.122.203:8420/mdb/protocols" | jq
```

**Pass criteria:**
- [ ] Returns 403 with `{"error": "authoring_disabled", "hint": "set env WALLAC_ENABLE_PROTOCOL_AUTHORING=true..."}`
- [ ] No protocol was inserted (verify with GET)

### Test 8: Abort during generated run

**Goal:** Verify abort works during a generated protocol run.

**Steps:**
1. Create and submit a generated protocol job (same as Test 4)
2. Wait for the run to start (state → ``running``)
3. Call ``POST /jobs/{job_id}/abort`` on the bridge HTTP API:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://lambdabiolab-computer:8423/jobs/{job_id}/abort" | jq
   ```

4. The bridge forwards the abort to vm-agent ``POST /runs/{id}/abort``
   and polls until the instrument stops. If the run is younger than the
   vm-agent's 60 s minimum abort age, the bridge retries on the next poll
   cycle.

**Pass criteria:**
- [ ] ``POST /jobs/{job_id}/abort`` returns ``{"abort_requested": true}``
- [ ] vm-agent ``abort_run()`` is called (verify via vm-agent logs)
- [ ] Job state → ``aborted`` (or ``failed`` if abort was rejected)
- [ ] Temporary cloned protocol is cleaned up (``_cleanup_cloned_protocol`` is called in ``finally``; verify via executor logs)
- [ ] Factory template protocol is never deleted
- [ ] Event log records the abort sequence

## Production enablement checklist

Before enabling generated-protocol authoring in production:

- [ ] All tests 1-8 pass
- [ ] OEM OD comparison matches (Test 5)
- [ ] Operator has reviewed and approved the generated protocol format
- [ ] vm-agent has ``WALLAC_ENABLE_PROTOCOL_AUTHORING=true`` set (in ``C:\install\run_agent.bat`` or equivalent)
- [ ] Protocol group `eLabFTW Generated` exists in the MDB
- [ ] Template protocols are installed and verified
- [ ] Backup directory `C:\Users\Public\mdb_backups` is accessible
- [ ] Bridge daemon is running as a systemd service

## Notes

- **Plate presence:** The Victor2 COM API does not expose plate-loaded status.
  The operator must verify the plate is loaded before submitting a job.
- **Abort latency:** ``POST /jobs/{id}/abort`` is sub-second on the bridge.
  The vm-agent enforces a 60 s minimum abort age (returns 425 "too early" if
  the run is too young); the bridge retries on the next poll cycle. Emergency
  stops use the physical button on the instrument.
- **Cleanup:** Temporary ``ELAB-Run-*`` clones are cleaned up automatically
  after execution. The ``DELETE /mdb/protocols/{id}`` endpoint is available
  for ad-hoc removal of any leftover protocols.
- **No auto-restore:** MDB backups are never automatically restored in v1.
