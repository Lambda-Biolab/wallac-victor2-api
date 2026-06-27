#!/usr/bin/env python3
"""Create a dry-run fixture: a signed Automation Job bundle for bridge validation.

This script creates a complete signed bundle (Method, Layout, Analysis, Job)
in eLabFTW, ready for the bridge to discover and validate in dry-run mode
(``WALLAC_DRY_RUN=true``). The bridge will validate signatures, hashes, and
lifecycle states without touching the instrument.

Usage::

    # Create the fixture (requires eLabFTW API key + signing passphrase)
    ELABFTW_URL=https://antonios-beast:3148 \\
    ELABFTW_API_KEY=4-l4mbd4... \\
    SIGNING_PASSPHRASE=wallac2024 \\
    python tools/create_dry_run_fixture.py

    # Then start the bridge in dry-run mode
    WALLAC_DRY_RUN=true python main.py

    # The bridge will claim the job, validate the bundle, upload a
    # dry_run_report.json, and mark the job as completed — all without
    # touching the instrument.

Environment variables:

    ELABFTW_URL          eLabFTW base URL (default: https://localhost:3148)
    ELABFTW_API_KEY      eLabFTW API key (required)
    SIGNING_PASSPHRASE   eLabFTW signing passphrase (required)
    ELABFTW_CATEGORY     Automation Job category ID (default: 21)

The script is idempotent: it checks for an existing fixture by title and
skips creation if one is found.
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

# Category IDs (items_types template IDs)
METHOD_CATEGORY = 10
LAYOUT_CATEGORY = 11
ANALYSIS_CATEGORY = 12
JOB_CATEGORY = 9

FIXTURE_TITLE = "DRY-RUN FIXTURE — DO NOT EXECUTE"


def _make_session(base_url: str, api_key: str) -> tuple[str, ssl.SSLContext]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return base_url.rstrip("/"), ctx


def _request(
    base: str,
    api_key: str,
    ctx: ssl.SSLContext,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", api_key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            content = resp.read()
            if not content:
                loc = resp.headers.get("Location") or ""
                if loc:
                    return {"_location": loc}
                return None
            return json.loads(content)
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):
            detail = e.read().decode()[:300]
        print(f"  API {method} {path} -> {e.code}: {detail}", file=sys.stderr)
        raise


def _find_existing(base: str, api_key: str, ctx: ssl.SSLContext, title: str) -> int | None:
    """Check if a fixture with this title already exists."""
    items = _request(base, api_key, ctx, "GET", f"/items?cat={JOB_CATEGORY}")
    for item in items or []:
        if item.get("title") == title:
            return item["id"]
    return None


def _normalize_metadata(extra_fields: dict[str, Any]) -> str:
    return json.dumps({"extra_fields": extra_fields})


def _patch_metadata(
    base: str, api_key: str, ctx: ssl.SSLContext, item_id: int, extra_fields: dict[str, Any]
) -> None:
    # Read-merge-write: GET current metadata, merge, PATCH
    item = _request(base, api_key, ctx, "GET", f"/items/{item_id}")
    meta = item.get("metadata")
    if isinstance(meta, str):
        meta = json.loads(meta)
    if not isinstance(meta, dict):
        meta = {}
    ef = meta.get("extra_fields", {})
    ef.update(extra_fields)
    meta["extra_fields"] = ef
    _request(base, api_key, ctx, "PATCH", f"/items/{item_id}", {"metadata": json.dumps(meta)})


def _sign_entity(
    base: str, api_key: str, ctx: ssl.SSLContext, item_id: int, passphrase: str
) -> None:
    _request(
        base,
        api_key,
        ctx,
        "PATCH",
        f"/items/{item_id}",
        {"action": "sign", "passphrase": passphrase, "meaning": 10},
    )


def _upload_file(
    base: str, api_key: str, ctx: ssl.SSLContext, item_id: int, filename: str, content: bytes
) -> int:
    """Upload a file attachment. Returns the upload ID."""
    boundary = "----dry-run-fixture-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(f"{base}/items/{item_id}/uploads", data=body, method="POST")
    req.add_header("Authorization", api_key)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, context=ctx) as resp:
        loc = resp.headers.get("Location") or ""
        # Upload ID is the last numeric segment
        parts = loc.rstrip("/").rsplit("/", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
        return 0


def main() -> None:
    base_url = os.environ.get("ELABFTW_URL", "https://localhost:3148")
    api_key = os.environ.get("ELABFTW_API_KEY", "")
    passphrase = os.environ.get("SIGNING_PASSPHRASE", "")

    if not api_key:
        print("ERROR: ELABFTW_API_KEY is required", file=sys.stderr)
        sys.exit(1)
    if not passphrase:
        print("ERROR: SIGNING_PASSPHRASE is required", file=sys.stderr)
        sys.exit(1)

    base, ctx = _make_session(base_url, api_key)

    # Check for existing fixture
    existing = _find_existing(base, api_key, ctx, FIXTURE_TITLE)
    if existing:
        print(f"Fixture already exists: item #{existing}")
        print(f"  Title: {FIXTURE_TITLE}")
        print("  To re-run dry-run validation, set this job to 'requested' and")
        print("  start the bridge with WALLAC_DRY_RUN=true")
        sys.exit(0)

    print("Creating dry-run fixture bundle...")

    # --- 1. Create Method ---
    print("  Creating Method resource...", end=" ")
    method_spec = {
        "schema_name": "wallac.method",
        "schema_version": 1,
        "name": "Dry-run OD600",
        "mode": "photometry",
        "plate_type": "96-well",
        "photometry": {
            "filter_id": "P610",
            "filter_name": "610nm",
            "read_time_seconds": 0.1,
        },
    }
    method_bytes = json.dumps(method_spec, sort_keys=True, separators=(",", ":")).encode()
    import hashlib

    method_hash = hashlib.sha256(method_bytes).hexdigest()

    method_id = _request(
        base, api_key, ctx, "POST", "/items", {"type": METHOD_CATEGORY, "title": "Dry-run Method"}
    )
    if isinstance(method_id, dict) and "_location" in method_id:
        method_id = int(method_id["_location"].rstrip("/").rsplit("/", 1)[-1])
    elif isinstance(method_id, dict) and "id" in method_id:
        method_id = int(method_id["id"])
    print(f"item #{method_id}")

    method_upload_id = _upload_file(base, api_key, ctx, method_id, "method.json", method_bytes)
    _patch_metadata(
        base,
        api_key,
        ctx,
        method_id,
        {
            "Method hash": {"type": "text", "value": method_hash},
            "Method JSON attachment ID": {"type": "text", "value": str(method_upload_id)},
            "Lifecycle state": {"type": "text", "value": "signed/active", "readonly": True},
        },
    )
    _sign_entity(base, api_key, ctx, method_id, passphrase)
    print(f"    Signed, hash={method_hash[:16]}...")

    # --- 2. Create Layout ---
    print("  Creating Layout resource...", end=" ")
    wells = [{"well_name": f"A{c}", "role": "measured"} for c in range(1, 13)]
    wells += [{"well_name": f"B{c}", "role": "blank"} for c in range(1, 4)]
    layout_spec = {
        "schema_name": "wallac.layout",
        "schema_version": 1,
        "plate_type": "96-well",
        "wells": wells,
    }
    layout_bytes = json.dumps(layout_spec, sort_keys=True, separators=(",", ":")).encode()
    layout_hash = hashlib.sha256(layout_bytes).hexdigest()

    layout_id = _request(
        base, api_key, ctx, "POST", "/items", {"type": LAYOUT_CATEGORY, "title": "Dry-run Layout"}
    )
    if isinstance(layout_id, dict) and "_location" in layout_id:
        layout_id = int(layout_id["_location"].rstrip("/").rsplit("/", 1)[-1])
    elif isinstance(layout_id, dict) and "id" in layout_id:
        layout_id = int(layout_id["id"])
    print(f"item #{layout_id}")

    layout_upload_id = _upload_file(base, api_key, ctx, layout_id, "layout.json", layout_bytes)
    _patch_metadata(
        base,
        api_key,
        ctx,
        layout_id,
        {
            "Layout hash": {"type": "text", "value": layout_hash},
            "Layout JSON attachment ID": {"type": "text", "value": str(layout_upload_id)},
            "Lifecycle state": {"type": "text", "value": "signed/active", "readonly": True},
        },
    )
    _sign_entity(base, api_key, ctx, layout_id, passphrase)
    print(f"    Signed, hash={layout_hash[:16]}...")

    # --- 3. Create Analysis ---
    print("  Creating Analysis resource...", end=" ")
    analysis_spec = {
        "schema_name": "wallac.analysis",
        "schema_version": 1,
        "blank_subtraction": {"enabled": True, "blank_wells": ["B1", "B2", "B3"]},
        "replicate_aggregation": {"groups": ["A"], "statistics": ["mean", "sd", "cv"]},
    }
    analysis_bytes = json.dumps(analysis_spec, sort_keys=True, separators=(",", ":")).encode()
    analysis_hash = hashlib.sha256(analysis_bytes).hexdigest()

    analysis_id = _request(
        base,
        api_key,
        ctx,
        "POST",
        "/items",
        {"type": ANALYSIS_CATEGORY, "title": "Dry-run Analysis"},
    )
    if isinstance(analysis_id, dict) and "_location" in analysis_id:
        analysis_id = int(analysis_id["_location"].rstrip("/").rsplit("/", 1)[-1])
    elif isinstance(analysis_id, dict) and "id" in analysis_id:
        analysis_id = int(analysis_id["id"])
    print(f"item #{analysis_id}")

    analysis_upload_id = _upload_file(
        base, api_key, ctx, analysis_id, "analysis.json", analysis_bytes
    )
    _patch_metadata(
        base,
        api_key,
        ctx,
        analysis_id,
        {
            "Analysis hash": {"type": "text", "value": analysis_hash},
            "Analysis JSON attachment ID": {"type": "text", "value": str(analysis_upload_id)},
            "Lifecycle state": {"type": "text", "value": "signed/active", "readonly": True},
        },
    )
    _sign_entity(base, api_key, ctx, analysis_id, passphrase)
    print(f"    Signed, hash={analysis_hash[:16]}...")

    # --- 4. Create Automation Job ---
    print("  Creating Automation Job...", end=" ")
    job_spec = {
        "schema_name": "wallac.job",
        "schema_version": 1,
        "execution_mode": "generated_protocol",
        "method": {
            "object_id": method_id,
            "hash": method_hash,
            "json_attachment_id": method_upload_id,
        },
        "layout": {
            "source": "reusable",
            "hash": layout_hash,
            "json_attachment_id": layout_upload_id,
            "object_id": layout_id,
        },
        "analysis": {
            "object_id": analysis_id,
            "hash": analysis_hash,
            "json_attachment_id": analysis_upload_id,
        },
    }
    job_bytes = json.dumps(job_spec, sort_keys=True, separators=(",", ":")).encode()
    job_hash = hashlib.sha256(job_bytes).hexdigest()

    job_id = _request(
        base, api_key, ctx, "POST", "/items", {"type": JOB_CATEGORY, "title": FIXTURE_TITLE}
    )
    if isinstance(job_id, dict) and "_location" in job_id:
        job_id = int(job_id["_location"].rstrip("/").rsplit("/", 1)[-1])
    elif isinstance(job_id, dict) and "id" in job_id:
        job_id = int(job_id["id"])
    print(f"item #{job_id}")

    job_upload_id = _upload_file(base, api_key, ctx, job_id, "job.json", job_bytes)
    _patch_metadata(
        base,
        api_key,
        ctx,
        job_id,
        {
            "Automation service": {"type": "select", "value": "wallac_victor2"},
            "Execution mode": {"type": "select", "value": "generated_protocol"},
            "Method reference": {"type": "text", "value": str(method_id)},
            "Method hash": {"type": "text", "value": method_hash},
            "Layout reference": {"type": "text", "value": str(layout_id)},
            "Layout hash": {"type": "text", "value": layout_hash},
            "Analysis reference": {"type": "text", "value": str(analysis_id)},
            "Analysis hash": {"type": "text", "value": analysis_hash},
            "Job JSON attachment ID": {"type": "text", "value": str(job_upload_id)},
            "Job hash": {"type": "text", "value": job_hash},
            "Requested action": {"type": "select", "value": "submit"},
            "Automation state": {"type": "select", "value": "requested"},
            "Lifecycle state": {"type": "text", "value": "signed/active", "readonly": True},
        },
    )
    _sign_entity(base, api_key, ctx, job_id, passphrase)
    print(f"    Signed, hash={job_hash[:16]}...")

    print()
    print(f"Fixture created: Automation Job #{job_id}")
    print(f"  Title: {FIXTURE_TITLE}")
    print(f"  Method:   #{method_id} (hash {method_hash[:16]}...)")
    print(f"  Layout:   #{layout_id} (hash {layout_hash[:16]}...)")
    print(f"  Analysis: #{analysis_id} (hash {analysis_hash[:16]}...)")
    print()
    print("To validate with the bridge in dry-run mode:")
    print("  WALLAC_DRY_RUN=true python main.py")
    print()
    print("The bridge will:")
    print("  1. Discover and claim the job")
    print("  2. Validate all signatures and hashes")
    print("  3. Upload a dry_run_report.json")
    print("  4. Mark the job as completed")
    print("  5. NOT touch the instrument")


if __name__ == "__main__":
    main()
