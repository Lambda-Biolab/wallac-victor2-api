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

## Victor2 instrument — hardware notes

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
