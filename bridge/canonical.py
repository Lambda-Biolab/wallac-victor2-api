"""Deterministic canonicalization and SHA-256 hash helpers for signed specs.

The backend, not the browser, is the canonicalization authority.  This module
produces stable, byte-for-byte reproducible JSON from any dict so that:

  - the same logical content always produces the same bytes;
  - SHA-256 hashes computed over those bytes are reproducible and comparable;
  - the bridge can download a signed attachment, hash the exact bytes, compare
    to the signed metadata hash, and fail closed on any mismatch before parsing.

Canonicalization rules (from docs/plans/wallac-protocol-authoring.md
"Deterministic serialization"):

  - UTF-8 bytes;
  - sorted keys (recursively, at every nesting level);
  - no insignificant whitespace (separators ``","`` and ``":"``);
  - non-ASCII characters preserved as UTF-8 bytes (``ensure_ascii=False``);
  - SHA-256 computed over the exact attached bytes, returned as a lowercase
    hex string.

Supported v1 schema names are defined in :mod:`bridge.schemas`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .errors import CANONICAL_HASH_MISMATCH, BridgeError

# JSON serialization parameters that produce deterministic, compact output.
# separators=(",", ":") removes all insignificant whitespace.
# sort_keys=True sorts at every nesting level.
# ensure_ascii=False preserves UTF-8 characters as bytes rather than
# escaping them to \uXXXX, so the hash is over the true UTF-8 representation.
_CANONICAL_JSON_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
}


def canonicalize(data: dict[str, Any]) -> bytes:
    """Serialize a dict to deterministic, byte-stable JSON.

    The same dict always produces the same bytes, making SHA-256 hashes
    reproducible across processes, platforms, and time.

    Args:
        data: Any JSON-serializable dict.  Must contain ``schema_name`` and
            ``schema_version`` at the top level for executable specs, but
            this function does not enforce that — schema validation is
            :mod:`bridge.schemas`'s responsibility.

    Returns:
        UTF-8 encoded JSON bytes with sorted keys and no whitespace.
    """
    return json.dumps(data, **_CANONICAL_JSON_KWARGS).encode("utf-8")


def compute_hash(data: bytes) -> str:
    """Compute the SHA-256 hash of raw bytes.

    Args:
        data: Exact bytes (typically the output of :func:`canonicalize` or
            the raw bytes downloaded from an eLabFTW attachment).

    Returns:
        Lowercase hex string, 64 characters.
    """
    return hashlib.sha256(data).hexdigest()


def canonicalize_and_hash(data: dict[str, Any]) -> tuple[bytes, str]:
    """Canonicalize a dict and compute its SHA-256 hash in one call.

    Returns:
        ``(canonical_bytes, sha256_hex)``.
    """
    canonical_bytes = canonicalize(data)
    return canonical_bytes, compute_hash(canonical_bytes)


def verify_hash(data: bytes, expected_hash: str) -> None:
    """Verify that ``data`` hashes to ``expected_hash``.

    Uses :func:`hmac.compare_digest` for timing-safe comparison.

    Args:
        data: The raw bytes downloaded from an eLabFTW attachment.
        expected_hash: The signed SHA-256 hex string from eLabFTW metadata.

    Raises:
        BridgeError: with code ``CANONICAL_HASH_MISMATCH`` if the hashes do
            not match.  This is a fail-closed check — the bridge must not
            parse or execute JSON whose hash does not match the signed value.
    """
    actual_hash = compute_hash(data)
    if not hmac.compare_digest(actual_hash, expected_hash.lower()):
        raise BridgeError(
            code=CANONICAL_HASH_MISMATCH,
            human_message=(
                "Canonical hash mismatch: the downloaded attachment bytes do "
                "not match the signed SHA-256 hash."
            ),
            operator_hint=(
                "The attachment may have been replaced or corrupted after "
                "signing.  Re-sign the object or restore the original "
                "attachment."
            ),
            retryable=False,
            details={
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            },
        )
