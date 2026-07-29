"""Idempotency-key helpers for writeback steps.

Every writeback action has a deterministic key derived from the bridge
job_id plus the action name. eLabFTW calls then carry that key as a
metadata tag, and the bridge uses it to detect duplicate upload
attempts after ambiguous HTTP responses.

Naming convention::

    jobs/<job_id>/artifacts/<sha256>.json   body section marker
    wallac.<job_id>.<action>               eLabFTW metadata tag

The bridge never relies on body-equality alone to detect duplicates;
the metadata tag is the canonical idempotency record.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def step_idempotency(job_id: str, action: str, payload_hash: str) -> str:
    """Stable idempotency token for a writeback step."""
    return f"{job_id}:{action}:{payload_hash}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deterministic_attachment_name(job_id: str, kind: str, sha256_hex: str) -> str:
    """A filename safe to reuse across retries.

    The filename includes the SHA-256 so the bridge can detect when
    eLabFTW already has the artifact and skip the upload (idempotent
    writeback).
    """
    return f"wallac_{job_id}_{kind}_{sha256_hex[:16]}.bin"


def metadata_tag(step_idempotency: str) -> str:
    """Stable metadata key we attach to the eLabFTW experiment.

    Issue #44 requires a stable bridge correlation ID in eLabFTW
    metadata. The metadata key is namespaced under ``wallac.bridge.``
    so it does not collide with future operators.
    """
    return f"wallac.bridge.step.{step_idempotency}"


def make_attachment_meta(name: str, sha256_hex: str, comment: str) -> dict[str, Any]:
    """Metadata record we write into the experiment for each attachment."""
    return {"name": name, "sha256": sha256_hex, "comment": comment}


def json_dumps_compact(obj: Any) -> str:
    """Single-line JSON encoder for the bridge correlation markers.

    eLabFTW metadata fields round-trip through JSON; we keep the
    deterministic encoding compact so the metadata diff between two
    retries of the same step is empty.
    """
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)
