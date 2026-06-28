# Agent Learnings — wallac-victor2-api

Concise, laser-focused patterns and gotchas discovered during development.

## eLabFTW API v2 — gotchas discovered during live e2e testing

### 1. Item creation: use `type`, not `category`

The `items` table has a `category` FK to `items_categories`, but the
`items_types` API creates templates without always creating the corresponding
`items_categories` entry. POSTing `{"category": 10}` fails with FK constraint
error. Use `{"type": 10}` instead — this tells eLabFTW to create the item from
the template, which handles the category linkage internally.

### 2. Item PATCH: no `action` field

The `items_types` PATCH endpoint uses `{"action": "update", ...}`, but the
`items` PATCH endpoint does NOT accept `action`. Sending it returns 400
"Invalid update target." Just send `{"metadata": ...}` directly.

### 3. Upload POST returns 201 with Location header, empty body

Both `POST /items` and `POST /items/{id}/uploads` return HTTP 201 with a
`Location` header pointing to the new resource, but the response body is
empty. Parse the ID from the Location URL:
`int(loc.rstrip("/").rsplit("/", 1)[-1])`.

### 4. Upload GET returns JSON metadata by default, not file content

`GET /items/{id}/uploads/{upload_id}` returns the upload metadata as JSON.
To download the actual file content, use `?format=binary`:
`GET /items/{id}/uploads/{upload_id}?format=binary`.

### 5. Metadata must be `json.dumps()`'d

When PATCHing metadata, the `metadata` field must be a JSON string (not a
dict). Passing a raw dict causes HTTP 500. Use:
`body={"metadata": json.dumps(meta, ensure_ascii=False)}`.

### 6. `items_types` vs `items_categories`

eLabFTW has two related tables:
- `items_types` — resource category templates (what the API creates/patches)
- `items_categories` — web UI categories (what `items.category` FK references)

Creating an `items_type` via the API does NOT always create the
corresponding `items_categories` entry. This is why `?cat=` filtering may not
work for API-created categories. Use `?type=` instead.

## DAO/comtypes — binary field gotchas (Jet OLE Object columns)

### 1. DAO AddNew/Update fails with comtypes on Jet

Both `ProtocolGroup` and `AssayProtocol` inserts fail when using DAO
Recordset `AddNew()` / `Update()`. Use SQL `INSERT INTO` instead.

### 2. comtypes returns OLE Object fields as tuples of ints

Reading a binary field (e.g. `PlateMap`) via DAO returns a `tuple` of
ints, not `bytes`. Reading `NormalizationInfo` returns `None` when the
field is NULL.

### 3. DAO AppendChunk accepts only `array.array('B', ...)`

comtypes cannot marshal `bytes` to a COM VARIANT — `AppendChunk(bytes)`
raises `ArgumentError`. `list` raises `COMError`. Only `array.array('B')`
works with `AppendChunk`.

### 4. AppendChunk fails on NULL OLE Object fields

DAO `AppendChunk` raises "Data type conversion error" when the target
field was initialized as NULL by a prior SQL INSERT. Direct assignment
(`fld.Value = arr`) also fails on NULL fields.

**Solution:** clone the entire template row via `INSERT INTO ... SELECT`
(which copies binary fields in one SQL step), then `UPDATE` only the
non-binary override fields. This avoids AppendChunk entirely for
protocol generation.

See `op_mdb_insert_protocol()` in `vm-agent/agent.py`.

## vm-agent result format — well name normalization

### 1. vm-agent returns `{"well": "A01", ...}`, not `{"well_name": ...}`

The vm-agent `_normalize_well()` produces keys `well`, `od`, `counts`.
Layout/analysis specs use `well_name`. The bridge must check both keys
via a `_well_key()` helper.

### 2. vm-agent zero-pads well names (A01, A02, ..., A12)

The vm-agent returns `A01`, `A02`, etc. (zero-padded), but layout specs
use canonical non-padded names (`A1`, `A2`, ..., `A12`). The bridge
normalizes both sides to non-padded form via `_normalize_well_name()`.

See `bridge/execution.py` (`_well_key`, `_normalize_well_name`) and
`bridge/analysis.py` (`_load_raw`).

## Jet SQL — wildcard and quoting gotchas

### 1. `LIKE` uses `*` and `?`, NOT ANSI `%` and `_`

Jet/DAO SQL's `LIKE` operator uses the legacy Access wildcards:
`*` (any sequence), `?` (single char), `#` (single digit), `[chars]`
(character class). The ANSI wildcards (`%`, `_`) are treated as
**literal characters** — `LIKE "ELAB-Job-%"` matches the literal string
`ELAB-Job-%` and returns 0 rows.

Use `ALIKE` for ANSI-standard wildcards: `ALIKE "ELAB-Job-%"` works.
Or use Jet wildcards: `LIKE "ELAB-Job-*"`.

### 2. Double-quoted strings in `/mdb/query` SQL are accepted as string literals

Contrary to expectation, Jet accepts `"..."` as string-literal delimiters
in `WHERE` clauses (in addition to the standard `'...'`). Either works
in the vm-agent `/mdb/query` endpoint. The vm-agent passes SQL straight
to `db.OpenRecordset(sql)` with no sanitization.

### 3. `/mdb/protocols?name=` is EXACT match only

The vm-agent `GET /mdb/protocols?name=<n>` calls
`op_mdb_find_protocol_by_name(name)` which does exact match. No glob,
no prefix, no wildcard. Use `POST /mdb/query` with a `LIKE`/`ALIKE`
clause for pattern matching.

## Generated-protocol cleanup — in-memory gap (FIXED)

`GeneratedProtocolManager.cleanup_terminal()` previously iterated only the
in-memory `self._generated` dict — empty after a bridge restart, so
cleanup dry-run silently reported nothing-to-do even though `ELAB-Job-*`
protocols existed in the MDB.

**Fixed (2026-06-27):** both `cleanup_terminal()` and `delete_protocol()`
now query the MDB via `MdbClient.query("SELECT ... WHERE ProtName ALIKE 'ELAB-Job-%'")`
(ANSI wildcard for Jet SQL). Defense-in-depth: results are filtered again
by `GENERATED_NAME_PREFIX` before any delete, so factory presets and user
protocols can never be targeted. 7 new tests cover the restart scenario.


### Plate presence detection

The Victor2 COM automation API does **not** expose a "plate present" sensor.
The vm-agent `/health` and `/status` endpoints report instrument connection
state (Idle/Running/Error) but cannot detect whether a plate is physically
loaded. Plate-loaded verification is an **operator responsibility**.

### Protocol well selection (PlateMap)

The `AssayProtocol.PlateMap` field is a binary blob encoding which wells to
measure:
- 12-byte header: plate count (4 bytes LE), columns (4 bytes LE), rows (4 bytes LE)
- 96 bytes: one per well, `01` = measured, `00` = skipped
- Row-major order: A1, A2, ..., A12, B1, ..., H12

### Photometry label/filter system

Each photometry protocol references a `Photometry` label row via
`MeasSequence` (e.g. `M:0;L:2200001;`). The label row specifies:
- `CWLampFilterID` — the filter ID (e.g. 15 = P610, the "600nm" filter)
- `MeasTime` — read time in seconds (0.1 or 1.0)
- `FlashLampFilter` — lamp filter (3 = standard)

### Installed photometry filters

| FilterID | Name | Wavelength | Slot |
|---|---|---|---|
| 8 | P405 | 405nm | 3/1 |
| 9 | P450 | 450nm | 3/2 |
| 10 | P490 | 490nm | 3/3 |
| 14 | P690 | 690nm | 3/4 |
| 15 | P610 | 610nm | 3/7 |

### Custom protocols created on this instrument

| AssayProtID | Name | LabelID | Filter | MeasTime | Wells |
|---|---|---|---|---|---|
| 2000000 | Absorbance @ 600 (1.0s) | 2200000 | P610 | 1.0s | Row A (8 wells) |
| 2000001 | Absorbance @ 600 (0.1s) | 2200001 | P610 | 0.1s | All 96 |

### Path length and volume

Standard 96-well plates expect 200–300µL for proper photometry path length.
At 100µL, absorbance readings are roughly half of what they'd be at 200µL.
Phenol-red-level dye concentrations (e.g. DMEM) give OD600 ~0.03–0.05 at
200µL, which is near the instrument's detection floor for colorimetric use.
600nm photometry is primarily useful for turbidity (bacterial growth, OD600
0.1–1.0), not dilute colorimetric dyes.

## eLabFTW API key creation via database

### Creating a dedicated service user + API key

eLabFTW's `POST /apikeys` endpoint only creates keys for the *authenticated*
user — you can't create a key for another user via the API. To provision a
dedicated service identity:

1. **Create user via API:** `POST /users` with
   `{"firstname", "lastname", "email", "team", "usergroup"}`. Returns 201
   with Location header. User is created with `validated=1` and added to
   `users2teams`.

2. **Remove sysadmin (least privilege):** `UPDATE users SET is_sysadmin = 0
   WHERE userid = N;` via MySQL.

3. **Insert API key via DB:** The key format is `<api_keys.id>-<secret>`.
   The bcrypt hash must be of the **secret part only**, NOT the full key
   with the id prefix. eLabFTW splits the key by `-`, looks up the row by
   id, then calls `password_verify($secret, $hash)`.

   ```python
   import bcrypt, secrets
   secret = secrets.token_hex(24)
   hashed = bcrypt.hashpw(secret.encode(), bcrypt.gensalt(rounds=12)).decode()
   hashed_php = hashed.replace('$2b$', '$2y$', 1)  # PHP compatibility
   # INSERT INTO api_keys (name, hash, can_write, userid, team)
   #   VALUES ('service', '<hashed_php>', 1, <userid>, <team>);
   # Full key: <id>-<secret>
   ```

4. **Verify:** `curl -H "Authorization: <id>-<secret>" /api/v2/users/me`
   should return the service user's data.

**Gotcha:** Python's bcrypt generates `$2b$` hashes; PHP's `password_verify()`
accepts both `$2b$` and `$2y$`, but use `$2y$` for consistency with
eLabFTW's existing keys.

## VM Agent Restart — always use start_agent.bat (NEVER taskkill solo)

**Rule (MANDATORY, NON-NEGOTIABLE):** When restarting the vm-agent on the
Windows VM, **always run `C:\install\start_agent.bat`** — never call
`taskkill /F /IM python.exe` solo.

**Why:** the Wallac Victor2 instrument has a faulty lid sensor that
randomly pops a modal "Wallac 1420 Exception — LID OPEN ERROR" dialog
which **deadlocks every measurement** until dismissed. The
`lid_watcher.py` script polls for this dialog and auto-clicks Ignore.
It is the **only remediation** for this hardware defect. `start_agent.bat`
starts **both** `lid_watcher.py` (via `pythonw.exe`) AND `agent.py`. If
you kill `python.exe` / `pythonw.exe` directly without then running
`start_agent.bat`, the lid_watcher is left dead and the **next** lid
error will wedge the instrument until a human intervenes at the console.

**Operational checklist before any VM operation that touches instrument
state:**

1. VM SSH access is via **jump host**:
   `ssh -J antonio@lambdabiolab-computer lambda@192.168.122.203`
   (NOT `ssh lambdabiolab-computer` then `ssh lambda@…` — the host has
   no key for the VM, only the workstation's jump config works.)
2. To restart the agent, run `start_agent.bat` end-to-end via:
   `powershell -NoProfile -Command "Start-Process -FilePath C:\install\start_agent.bat -WindowStyle Hidden"`
3. After restart, **verify both** processes are alive:
   `Get-Process python,pythonw -ErrorAction SilentlyContinue`
   Expect **2 processes**: `python.exe` (agent) and `pythonw.exe` (lid_watcher).
   If only `python.exe` is alive, the lid_watcher has died — see below.
4. If `pythonw.exe` keeps dying after `start_agent.bat`: run `lid_watcher.py`
   directly with debug logging (`Get-Content C:\Users\Public\lid_watcher.log -Tail 20`)
   to diagnose. Known intermittent issue on Win7 + Python 3.8.

**Related rule:** The `start_agent.bat` kills ALL python.exe/pythonw.exe
processes on the VM before starting fresh. If you have other Python
scripts running, they will be killed too.

**Known cause of pythonw death:** Even when `lid_watcher.py` logs
`lid_watcher started` successfully, the `pythonw.exe` process can die
silently within a few seconds on Win7. The `python.exe` (console) variant
appears to survive. If `pythonw.exe` repeatedly dies, fall back to
running `lid_watcher.py` via `python.exe -WindowStyle Hidden` until
the root cause is resolved.

**File paths on the VM:**
- Agent: `C:\install\agent.py`
- Lid watcher: `C:\install\lid_watcher.py`
- Restart script: `C:\install\start_agent.bat` — ALWAYS USE THIS
- Full-stack cold start (incl. MlrMgr): `C:\install\start-stack.bat`
- Lid watcher log: `C:\Users\Public\lid_watcher.log`
- Abort flag (triggers ABORT click on next error): `C:\Users\Public\abort.flag`
- Git repo on host: `~/repos/wallac-victor2-api/`
- Git repo for ops: `~/repos/wallac-victor2-linux/` (see
  `host-config/VM-OPERATIONS.md` for full VM access reference)

## VM Process Spawning from SSH — use `wmic process call create`, NOT `Start-Process`

**Symptom:** `lid_watcher.py` (launched as `pythonw.exe` from an SSH
session) consistently died ~5-30 seconds after the SSH session closed.
`lid_watcher.log` showed `lid_watcher started` correctly, and the process
even did its job when alive (the log captured a real `auto-Ignored`
event), but the process was gone within seconds of every SSH disconnect.
Meanwhile, `start_agent.bat` launched at boot (from the Windows Startup
folder) kept `lid_watcher` alive indefinitely.

**Root cause:** SSH session teardown kills the entire process tree.
`Start-Process -FilePath pythonw.exe -ArgumentList lid_watcher.py`
spawned `pythonw.exe` as a child of the SSH-spawned PowerShell. When the
SSH channel closed, Windows tore down the whole session — including the
`lid_watcher` we just started. The Linux-side `nohup`/`setsid` reflex
doesn't apply on Win7; `Start-Process` does not detach the process from
the SMB session that spawned it.

The boot-launched `start-stack.bat` (in the Startup folder) has no SSH
parent session, so its `start "" pythonw.exe lid_watcher.py` invocations
survive. That is the only reason the system worked at all previously.

**Fix — use `wmic process call create`:**

```
ssh -J antonio@lambdabiolab-computer lambda@192.168.122.203 \
  'wmic process call create "C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\pythonw.exe C:\install\lid_watcher.py"'
```

`wmic process call create` runs the Win32 `Create()` method via the WMI
provider service, which is detached from the calling SSH session's
process tree. The new process survives SSH disconnect indefinitely.
Returns `ProcessId` and `ReturnValue=0` on success.

Same pattern for restarting the agent:

```
ssh -J antonio@lambdabiolab-computer lambda@192.168.122.203 \
  'wmic process call create "C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\python.exe C:\install\agent.py"'
```

**Verified:** Both processes (`python.exe` agent + `pythonw.exe`
lid_watcher) survived 15+ seconds after the SSH session closed, and
remained alive across subsequent SSH disconnects.

**Anti-patterns that DON'T work from SSH:**
- `Start-Process -FilePath pythonw.exe -ArgumentList lid_watcher.py` —
  killed when SSH session closes
- `Start-Process -FilePath python.exe -ArgumentList lid_watcher.py
  -WindowStyle Hidden` — same; killed on SSH teardown, despite the
  hidden window style
- Running `start_agent.bat` via `Start-Process -WindowStyle Hidden` —
  the bat's internal `start ""` calls are still children of the SSH
  session and die with it
- `python.exe lid_watcher.py` run in foreground — dies immediately when
  the SSH command times out or is cancelled

**Bottom line:** From within an SSH session to the VM, ALWAYS use
`wmic process call create` to spawn any process that needs to survive
the SSH disconnect. `Start-Process` does not detach on Win7 the way
`setsid`/`nohup` do on Linux.

**Also: `wmic process call create` does NOT kill existing processes.**
Unlike `start_agent.bat` which does `taskkill /F /IM python.exe` first,
`wmic` just spawns a new process. Before running it, you must
explicitly kill the prior instance or you'll end up with duplicate
`python.exe` processes (both binding to port 8420, second one fails to
listen). Use `taskkill /F /PID <pid>` on the specific PID, or run
`start_agent.bat` first to do the kill-then-spawn dance, then start
`lid_watcher` separately via `wmic`.
