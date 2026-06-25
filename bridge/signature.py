"""Minisign-compatible signature verification for eLabFTW Automation Jobs.

eLabFTW signs entities with Ed25519ph (pre-hashed) minisign-compatible
signatures.  When an operator signs an Automation Job, eLabFTW creates a
signature archive (zip) containing:

  - ``data.json`` — full JSON export of the entity at sign time
  - ``data.json.minisig`` — the minisign signature
  - ``key.pub`` — the signer's public key
  - ``verify.sh`` — a verification script

This module parses the signature archive, verifies the cryptographic
signature, extracts signer metadata from the trusted comment, and compares
the signed snapshot against the current entity state to detect post-signature
modifications.

Reference: https://doc.elabftw.net/docs/usage/user-guide/misc/
           eLabFTW-lambdabiolab/AGENT_LEARNINGS.md
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import Any

import nacl.encoding
import nacl.exceptions
import nacl.hash
import nacl.signing

from .models import RequestFields, SignatureInfo

# --- Minisign file parsing --------------------------------------------------


def parse_minisig(content: str) -> dict[str, Any]:
    """Parse a minisign signature file (``.minisig``) into its components.

    Format::

        untrusted comment: <arbitrary text>
        <base64(sig_algo || key_id || signature)>
        trusted comment: <json or text>
        <base64(global_signature)>

    Returns a dict with keys: ``sig_algo``, ``key_id``, ``signature``,
    ``trusted_comment``, ``global_signature``.
    """
    lines = content.strip().split("\n")
    if len(lines) < 4:
        raise ValueError("invalid minisig: expected at least 4 lines")

    # Line 0: untrusted comment (ignored)
    # Line 1: base64(ED + key_id + signature)
    sig_blob = base64.b64decode(lines[1])
    sig_algo = sig_blob[:2]  # b"ED" = Ed25519ph
    key_id = sig_blob[2:10]  # 8 bytes
    signature = sig_blob[10:]  # 64 bytes

    # Line 2: trusted comment
    trusted_comment = lines[2]
    prefix = "trusted comment: "
    if trusted_comment.startswith(prefix):
        trusted_comment = trusted_comment[len(prefix) :]

    # Line 3: base64(global_signature)
    global_signature = base64.b64decode(lines[3])

    return {
        "sig_algo": sig_algo,
        "key_id": key_id,
        "signature": signature,
        "trusted_comment": trusted_comment,
        "global_signature": global_signature,
    }


def parse_pubkey(content: str) -> dict[str, bytes]:
    """Parse a minisign public key file (``key.pub``) into its components.

    Format::

        untrusted comment: <arbitrary text>
        <base64(sig_algo || key_id || public_key)>

    Returns a dict with keys: ``sig_algo``, ``key_id``, ``public_key``.
    """
    lines = content.strip().split("\n")
    if len(lines) < 2:
        raise ValueError("invalid pubkey: expected at least 2 lines")

    blob = base64.b64decode(lines[1])
    sig_algo = blob[:2]  # b"Ed" = Ed25519
    key_id = blob[2:10]  # 8 bytes
    public_key = blob[10:]  # 32 bytes

    return {
        "sig_algo": sig_algo,
        "key_id": key_id,
        "public_key": public_key,
    }


# --- Cryptographic verification ---------------------------------------------


def verify_signature(message: bytes, minisig: dict[str, Any], pubkey: dict[str, bytes]) -> bool:
    """Verify an Ed25519ph (pre-hashed) minisign signature.

    eLabFTW uses Ed25519ph: the message is hashed with BLAKE2b (64-byte
    output, no key) before signing.  The global signature covers
    ``signature + trusted_comment``.

    Returns ``True`` if both the main signature and the global signature
    verify successfully.
    """
    # Hash the message with BLAKE2b (sodium_crypto_generichash equivalent)
    message_hash = nacl.hash.blake2b(message, digest_size=64, encoder=nacl.encoding.RawEncoder)

    verify_key = nacl.signing.VerifyKey(pubkey["public_key"])

    # Verify the main signature (over the hash, not the raw message)
    try:
        verify_key.verify(message_hash, minisig["signature"])
    except nacl.exceptions.BadSignatureError:
        return False

    # Verify the global signature (over signature + trusted_comment)
    global_message = minisig["signature"] + minisig["trusted_comment"].encode()
    try:
        verify_key.verify(global_message, minisig["global_signature"])
    except nacl.exceptions.BadSignatureError:
        return False

    return True


# --- Signer metadata extraction --------------------------------------------


def extract_signature_info(minisig: dict[str, Any]) -> SignatureInfo:
    """Extract signer metadata from the trusted comment JSON.

    The trusted comment is a JSON string with: firstname, lastname, email,
    created_at, site_url, created_by, meaning.
    """
    comment = minisig["trusted_comment"]
    try:
        data = json.loads(comment)
    except json.JSONDecodeError as e:
        raise ValueError(f"trusted comment is not valid JSON: {comment[:80]}") from e

    return SignatureInfo(
        signer_firstname=data.get("firstname", ""),
        signer_lastname=data.get("lastname", ""),
        signer_email=data.get("email", ""),
        signed_at=data.get("created_at", ""),
        meaning=data.get("meaning", ""),
        key_id=minisig["key_id"].hex(),
    )


# --- Signed snapshot comparison --------------------------------------------


def extract_signed_request_fields(data_json: bytes) -> RequestFields:
    """Extract request fields from the signed ``data.json`` snapshot.

    The ``data.json`` in the signature archive is a full JSON export of the
    entity at sign time.  Its ``metadata`` field contains ``extra_fields``
    (possibly double-encoded).
    """
    from .elabftw import extract_extra_fields

    data = json.loads(data_json)
    extra_fields = extract_extra_fields(data.get("metadata"))
    return RequestFields.from_extra_fields(extra_fields)


# --- Signature archive (zip) handling --------------------------------------


def parse_signature_archive(zip_bytes: bytes) -> tuple[bytes, dict[str, Any], dict[str, bytes]]:
    """Extract and parse components from a signature archive zip.

    Returns ``(data_json_bytes, minisig_dict, pubkey_dict)``.

    Raises ``KeyError`` if the archive is missing required files.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        data_json = zf.read("data.json")
        minisig_content = zf.read("data.json.minisig").decode()
        pubkey_content = zf.read("key.pub").decode()

    minisig = parse_minisig(minisig_content)
    pubkey = parse_pubkey(pubkey_content)
    return data_json, minisig, pubkey


def find_signature_upload(uploads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the signature archive upload in a list of uploads.

    eLabFTW stores the signature as an immutable upload with "signature" in
    the comment.  Returns the most recent matching upload, or ``None``.
    """
    candidates = [
        u
        for u in uploads
        if u.get("immutable") == 1 and "signature" in (u.get("comment") or "").lower()
    ]
    if not candidates:
        return None
    # Most recent first (highest id)
    return max(candidates, key=lambda u: u.get("id", 0))
