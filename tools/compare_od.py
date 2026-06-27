#!/usr/bin/env python3
"""Compare vm-agent/eLabFTW OD values against OEM MlrMgr export.

Usage:
    python3 compare_od.py <oem_export.csv> [--bridge <raw_results.json>]

The OEM export from MlrMgr is typically a CSV. This script auto-detects
the column layout (well address + OD value) and compares against the
bridge's raw_results.json (downloaded from eLabFTW or the vm-agent).

Pass criteria (Stage 7 Test 5):
  - OD values match within +/- 0.001
  - Well ordering matches (A1-H12, row-major)
  - No missing or extra wells
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

TOLERANCE = 0.001


def normalize_well(name: str) -> str:
    """A01 -> A1, B12 -> B12 (strip leading zero from column)."""
    name = name.strip()
    if len(name) >= 2 and name[0].isalpha():
        row = name[0].upper()
        col = name[1:].lstrip("0") or "0"
        return f"{row}{col}"
    return name


def load_bridge_results(path: str) -> dict[str, float]:
    """Load raw_results.json (list of {well, od, counts})."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "wells" in data:
        data = data["wells"]
    results = {}
    for w in data:
        well = normalize_well(w.get("well", ""))
        od = w.get("od")
        if od is not None:
            results[well] = float(od)
    return results


def load_oem_export(path: str) -> dict[str, float]:
    """Load OEM MlrMgr CSV export. Auto-detects well + OD columns."""
    results = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        print("ERROR: OEM export is empty", file=sys.stderr)
        sys.exit(1)

    # Try to detect header
    header = rows[0]
    well_col = None
    od_col = None

    for i, cell in enumerate(header):
        cell_lower = cell.strip().lower()
        if cell_lower in ("well", "position", "pos", "address"):
            well_col = i
        elif cell_lower in ("od", "od600", "absorbance", "abs", "value", "result"):
            od_col = i

    # If no header detected, try row-based detection (no header row)
    data_start = 0
    if well_col is None or od_col is None:
        # Try first data row as a sample
        for row_idx, row in enumerate(rows[:10]):
            for i, cell in enumerate(row):
                cell_stripped = cell.strip()
                # Check if this looks like a well address (A1-H12)
                if (
                    len(cell_stripped) >= 2
                    and cell_stripped[0].isalpha()
                    and cell_stripped[0].upper() in "ABCDEFGH"
                ):
                    well_col = i
                    # OD is likely the next numeric column
                    for j in range(i + 1, len(row)):
                        try:
                            float(row[j])
                            od_col = j
                            break
                        except ValueError:
                            continue
                    if od_col is not None:
                        data_start = 0  # no header
                        break
            if well_col is not None and od_col is not None:
                break

    if well_col is None or od_col is None:
        print(
            f"ERROR: could not auto-detect well/OD columns in OEM export.\n"
            f"Header was: {header}\n"
            f"First data row: {rows[1] if len(rows) > 1 else '(none)'}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Detected columns: well=col[{well_col}] od=col[{od_col}]")

    for row in rows[data_start:]:
        if len(row) <= max(well_col, od_col):
            continue
        well_raw = row[well_col].strip()
        if not well_raw or well_raw[0] not in "ABCDEFGHabcdefgh":
            continue
        try:
            od = float(row[od_col])
        except ValueError:
            continue
        well = normalize_well(well_raw)
        results[well] = od

    return results


def compare(bridge: dict[str, float], oem: dict[str, float]) -> bool:
    """Compare bridge vs OEM results. Returns True if all pass."""
    bridge_wells = set(bridge.keys())
    oem_wells = set(oem.keys())

    missing = bridge_wells - oem_wells
    extra = oem_wells - bridge_wells

    all_pass = True

    if missing:
        print(f"FAIL: {len(missing)} wells in bridge results but missing from OEM export")
        for w in sorted(missing)[:5]:
            print(f"  missing: {w}")
        all_pass = False

    if extra:
        print(f"FAIL: {len(extra)} wells in OEM export but not in bridge results")
        for w in sorted(extra)[:5]:
            print(f"  extra: {w}")
        all_pass = False

    # Compare OD values
    common = bridge_wells & oem_wells
    mismatches = []
    max_diff = 0.0

    for well in sorted(common):
        diff = abs(bridge[well] - oem[well])
        max_diff = max(max_diff, diff)
        if diff > TOLERANCE:
            mismatches.append((well, bridge[well], oem[well], diff))

    if mismatches:
        print(f"FAIL: {len(mismatches)} wells exceed tolerance +/-{TOLERANCE}")
        for well, b_od, o_od, diff in mismatches[:10]:
            print(f"  {well}: bridge={b_od:.4f} oem={o_od:.4f} diff={diff:.4f}")
        all_pass = False
    else:
        print(f"PASS: all {len(common)} wells match within +/-{TOLERANCE}")
        print(f"  max difference: {max_diff:.6f}")

    # Well ordering check (A1-H12, row-major)
    expected_order = []
    for row in "ABCDEFGH":
        for col in range(1, 13):
            expected_order.append(f"{row}{col}")

    bridge_order = [w for w in expected_order if w in bridge]
    oem_order = [w for w in expected_order if w in oem]

    if bridge_order == oem_order:
        print(f"PASS: well ordering matches (A1-H12, row-major)")
    else:
        print(f"FAIL: well ordering mismatch")
        all_pass = False

    print(f"\nSummary: {len(common)} wells compared, {len(mismatches)} mismatches, max_diff={max_diff:.6f}")
    return all_pass


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <oem_export.csv> [--bridge <raw_results.json>]", file=sys.stderr)
        sys.exit(1)

    oem_path = sys.argv[1]
    bridge_path = "/tmp/elabftw_raw_results.json"

    if "--bridge" in sys.argv:
        idx = sys.argv.index("--bridge")
        bridge_path = sys.argv[idx + 1]

    print(f"Loading bridge results: {bridge_path}")
    bridge = load_bridge_results(bridge_path)
    print(f"  {len(bridge)} wells loaded")

    print(f"Loading OEM export: {oem_path}")
    oem = load_oem_export(oem_path)
    print(f"  {len(oem)} wells loaded")

    print()
    success = compare(bridge, oem)
    print()
    print("RESULT: PASS" if success else "RESULT: FAIL")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
