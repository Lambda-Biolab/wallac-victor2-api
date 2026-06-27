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

## Generated-protocol cleanup — in-memory gap

`GeneratedProtocolManager.cleanup_terminal()` (line 381 in
`bridge/generated_protocols.py`) iterates only the in-memory
`self._generated` dict. After a bridge restart this dict is empty, so
cleanup dry-run silently reports nothing-to-do even though `ELAB-Job-*`
protocols exist in the MDB. To make Stage 7 Test 6 (cleanup dry-run)
pass against a restarted bridge, `cleanup_terminal()` must query the
MDB via `MdbClient.query("SELECT ... WHERE ProtName ALIKE 'ELAB-Job-%'")`
instead of iterating `self._generated`. (Predicate verified 2026-06-27:
generated group 10001 contains only ELAB-Job-* rows; 0 factory presets
in group 10001.)


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
