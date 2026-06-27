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
2. **Bridge daemon running** on `lambdabiolab-computer` (or `python3 main.py`)
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
  -d '{"sql":"SELECT AssayProtID, ProtName FROM AssayProtocol WHERE ProtName LIKE '\''ELAB-Job-%'\''"}' \
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
  -d '{"AssayProtID":2000099,"ProtName":"ELAB-Job-test-stage7","ProtNumber":99,"ProtVersion":1,"FactoryPreset":false,"ProtGroup":1}' \
  "http://192.168.122.203:8420/mdb/protocols" | jq

# Verify it exists
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols/2000099" | jq '.ProtName'

# Find by name
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols?name=ELAB-Job-test-stage7" | jq '.ProtName'

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

7. **Submit the job**: Set `Requested action = submit` in eLabFTW

8. **Monitor execution** via the dashboard at `http://lambdabiolab-computer:8421`

**Pass criteria:**
- [ ] Bridge claims the job within 5 seconds (state → `accepted`)
- [ ] Validation passes (signed bundle hashes verified)
- [ ] MDB backup created (check `MDB backup path` field in eLabFTW)
- [ ] Generated protocol created with name `ELAB-Job-<id>-<hash>`
- [ ] Post-write verification passes (protocol exists in MDB with correct name)
- [ ] Run starts by numeric AssayProtID (not by name)
- [ ] Run completes (state → `measured`)
- [ ] Raw results retrieved (96 wells)
- [ ] Result completeness check passes (all expected wells present)
- [ ] Analysis runs (blank subtraction, replicate aggregation, pass/fail)
- [ ] Artifacts uploaded to eLabFTW:
  - `raw_results.json`
  - `analyzed_wells.csv`
  - `replicate_summary.csv`
  - `replicate_summary.json`
  - `analysis_summary.json`
- [ ] Assay experiment created and linked to the Automation Job
- [ ] Job state → `completed`
- [ ] Event log posted as comment on the Automation Job

### Test 5: OEM OD comparison

**Goal:** Compare vm-agent-derived OD values against OEM export.

**Steps:**
1. After Test 4 completes, export the same run's results from the OEM GUI (MlrMgr)
2. Compare the OEM OD values with the `raw_results.json` OD values

**Pass criteria:**
- [ ] OD values match within ±0.001 (floating point tolerance)
- [ ] Well ordering matches (A1-H12, row-major)
- [ ] No missing or extra wells

### Test 6: Cleanup dry-run

**Goal:** Verify cleanup lists only eligible generated protocols.

```bash
# Dry-run cleanup
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.122.203:8420/mdb/protocols?name=ELAB-Job-*" | jq
```

**Pass criteria:**
- [ ] Only `ELAB-Job-*` protocols are listed
- [ ] Factory presets are NOT listed
- [ ] User GUI protocols are NOT listed

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
  -d '{"AssayProtID":2000098,"ProtName":"ELAB-Job-test-flag","ProtNumber":98,"ProtVersion":1,"FactoryPreset":false,"ProtGroup":1}' \
  "http://192.168.122.203:8420/mdb/protocols" | jq
```

**Pass criteria:**
- [ ] Returns 403 with `{"error": "authoring_disabled", "hint": "set env WALLAC_ENABLE_PROTOCOL_AUTHORING=true..."}`
- [ ] No protocol was inserted (verify with GET)

### Test 8: Abort during generated run

**Goal:** Verify abort works during a generated protocol run.

**Steps:**
1. Create and submit a generated protocol job (same as Test 4)
2. Wait for the run to start (state → `running`)
3. Set `Requested action = abort` in eLabFTW
4. Wait for the abort poller to detect it (≤5 seconds)

**Pass criteria:**
- [ ] Bridge detects abort within 5 seconds
- [ ] vm-agent abort_run() is called
- [ ] Job state → `aborted` (or `unknown_requires_operator_review` if abort fails)
- [ ] Generated protocol is NOT automatically deleted
- [ ] Event log records the abort

## Production enablement checklist

Before enabling generated-protocol authoring in production:

- [ ] All tests 1-8 pass
- [ ] OEM OD comparison matches (Test 5)
- [ ] Operator has reviewed and approved the generated protocol format
- [ ] `WALLAC_ENABLE_PROTOCOL_AUTHORING=true` set in `/etc/wallac-bridge/bridge.env`
- [ ] vm-agent has `WALLAC_ENABLE_PROTOCOL_AUTHORING=true` set
- [ ] Protocol group `eLabFTW Generated` exists in the MDB
- [ ] Template protocols are installed and verified
- [ ] Backup directory `C:\Users\Public\mdb_backups` is accessible
- [ ] Bridge daemon is running as a systemd service
- [ ] Dashboard is accessible to operators

## Notes

- **Plate presence:** The Victor2 COM API does not expose plate-loaded status.
  The operator must verify the plate is loaded before submitting a job.
- **Abort latency:** eLabFTW abort is non-real-time (5-15s latency). Emergency
  stops use the physical button on the instrument.
- **Generated protocol cleanup:** Is operator/admin-only maintenance, never
  automatic. Generated protocols are preserved for audit until explicit cleanup.
- **No auto-restore:** MDB backups are never automatically restored in v1.
