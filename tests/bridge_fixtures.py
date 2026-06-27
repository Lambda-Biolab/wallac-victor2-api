"""Test fixtures for the Wallac bridge: mock eLabFTW client + signature helpers.

The mock client implements :class:`bridge.elabftw.ElabftwInterface` with
in-memory state so tests can set up exact scenarios for the 5 acceptance
criteria from issue #2.

The signature generator creates real Ed25519ph minisign-compatible signatures
using PyNaCl, so the signature verification code is tested with real crypto.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import Any

import nacl.encoding
import nacl.hash
import nacl.signing

from bridge.elabftw import extract_extra_fields, get_field_value
from bridge.models import AutomationJob, RequestFields

# --- Minisign signature generation (for test fixtures) ---------------------


def generate_minisign_keypair() -> tuple[nacl.signing.SigningKey, bytes, bytes]:
    """Generate an Ed25519 keypair and minisign-format public key.

    Returns ``(signing_key, pubkey_bytes, pubkey_file_content)``.
    """
    signing_key = nacl.signing.SigningKey.generate()
    verify_key = signing_key.verify_key

    # 8-byte random key ID
    key_id = b"\x00" * 8  # deterministic for tests

    # Public key file: untrusted comment + base64(Ed + key_id + pubkey)
    pubkey_bytes = bytes(verify_key)
    blob = b"Ed" + key_id + pubkey_bytes
    pubkey_content = f"untrusted comment: test public key\n{base64.b64encode(blob).decode()}\n"

    return signing_key, pubkey_bytes, pubkey_content


def create_minisign_signature(
    signing_key: nacl.signing.SigningKey,
    message: bytes,
    *,
    firstname: str = "Test",
    lastname: str = "Operator",
    email: str = "test@example.org",
    meaning: str = "Approval",
) -> str:
    """Create a minisign-format signature string (Ed25519ph).

    Mirrors eLabFTW's SignatureHelper::serializeSignature:
      1. Hash the message with BLAKE2b (64-byte output)
      2. Sign the hash with Ed25519 (detached)
      3. Create a JSON trusted comment with signer metadata
      4. Sign (signature + trusted_comment) with Ed25519 (detached)
      5. Format as minisign signature file
    """
    # 1. Hash the message
    message_hash = nacl.hash.blake2b(message, digest_size=64, encoder=nacl.encoding.RawEncoder)

    # 2. Sign the hash (Ed25519ph)
    signature = signing_key.sign(message_hash).signature

    # 3. Trusted comment (JSON, like eLabFTW)
    key_id = b"\x00" * 8
    trusted_comment = json.dumps(
        {
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "created_at": "2026-06-25T12:00:00+00:00",
            "site_url": "https://localhost:3148",
            "created_by": "eLabFTW 50100",
            "meaning": meaning,
        }
    )

    # 4. Global signature (over signature + trusted_comment)
    global_message = signature + trusted_comment.encode()
    global_signature = signing_key.sign(global_message).signature

    # 5. Format as minisign file
    sig_blob = b"ED" + key_id + signature
    return (
        f"untrusted comment: test signature\n"
        f"{base64.b64encode(sig_blob).decode()}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{base64.b64encode(global_signature).decode()}\n"
    )


def create_signature_archive(
    signing_key: nacl.signing.SigningKey,
    data_json: bytes,
    pubkey_content: str,
    **signer_kwargs: str,
) -> bytes:
    """Create a signature archive zip (like eLabFTW's sign action).

    Contains: data.json, data.json.minisig, key.pub, verify.sh
    """
    minisig_content = create_minisign_signature(signing_key, data_json, **signer_kwargs)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", data_json)
        zf.writestr("data.json.minisig", minisig_content)
        zf.writestr("key.pub", pubkey_content)
        zf.writestr("verify.sh", "#!/bin/sh\nminisign -H -V -p key.pub -m data.json\n")
    return buf.getvalue()


# --- Mock eLabFTW client ---------------------------------------------------


def make_extra_fields(
    *,
    automation_service: str = "wallac_victor2",
    linked_experiment_id: str = "",
    protocol_name: str = "Absorbance @ 405",
    plate_layout_reference: str = "",
    expected_outputs: str = "",
    requested_action: str = "submit",
    automation_state: str = "requested",
    claimed_by: str = "",
    claimed_at: str = "",
    live_monitor: str = "",
    **extra: str,
) -> dict[str, Any]:
    """Build an extra_fields dict matching the Automation Job schema."""
    fields: dict[str, Any] = {
        "Automation service": {"type": "select", "value": automation_service},
        "Linked experiment ID": {"type": "text", "value": linked_experiment_id},
        "Protocol name": {"type": "text", "value": protocol_name},
        "Plate layout reference": {"type": "url", "value": plate_layout_reference},
        "Expected outputs": {"type": "text", "value": expected_outputs},
        "Requested action": {"type": "select", "value": requested_action},
        "Automation state": {"type": "select", "value": automation_state},
        "Claimed by": {"type": "text", "value": claimed_by},
        "Claimed at": {"type": "datetime-local", "value": claimed_at},
        "Live Monitor": {"type": "url", "value": live_monitor},
    }
    fields.update(extra)
    return fields


def make_data_json(item_id: int, extra_fields: dict[str, Any]) -> bytes:
    """Create a data.json snapshot (full JSON export) for a signature archive."""
    return json.dumps(
        {
            "id": item_id,
            "title": "Automation Job",
            "body": "<p>Test job</p>",
            "metadata": {"extra_fields": extra_fields},
        }
    ).encode()


class MockElabftwClient:
    """In-memory mock eLabFTW client for testing.

    Implements :class:`bridge.elabftw.ElabftwInterface`.  Stores items,
    uploads, and upload data in memory.  ``patch_metadata`` enforces
    state-transition guards for duplicate-claim prevention.
    """

    def __init__(self) -> None:
        # item_id -> {"id":..., "title":..., "metadata": {"extra_fields": {...}}}
        self._items: dict[int, dict[str, Any]] = {}
        # item_id -> list of upload dicts
        self._uploads: dict[int, list[dict[str, Any]]] = {}
        # (item_id, upload_id) -> bytes
        self._upload_data: dict[tuple[int, int], bytes] = {}
        # item_id -> list of comment strings
        self._comments: dict[int, list[str]] = {}
        self._next_upload_id = 1
        # If set, patch_metadata raises this to simulate transient failures
        self._patch_fail_countdown: int = 0
        # experiment_id -> {"title":..., "body":..., "links": [item_id, ...]}
        self._experiments: dict[int, dict[str, Any]] = {}
        self._next_experiment_id = 1

    # --- Setup helpers (for tests) ---

    def add_item(
        self,
        item_id: int,
        title: str = "Automation Job",
        extra_fields: dict[str, Any] | None = None,
    ) -> int:
        self._items[item_id] = {
            "id": item_id,
            "title": title,
            "metadata": {"extra_fields": extra_fields or {}},
        }
        self._uploads.setdefault(item_id, [])
        self._comments.setdefault(item_id, [])
        return item_id

    def set_patch_fail_countdown(self, count: int) -> None:
        """Configure the mock to fail the next N patch_metadata calls.

        Used to test write-back retry behavior.
        """
        self._patch_fail_countdown = count

    def add_signature_upload(
        self, item_id: int, archive_bytes: bytes, comment: str = "Signature archive"
    ) -> int:
        upload_id = self._next_upload_id
        self._next_upload_id += 1
        self._uploads.setdefault(item_id, []).append(
            {
                "id": upload_id,
                "comment": comment,
                "immutable": 1,
                "state": 1,  # archived
                "item_id": item_id,
            }
        )
        self._upload_data[(item_id, upload_id)] = archive_bytes
        return upload_id

    # --- ElabftwInterface implementation ---

    def list_automation_jobs(self) -> list[AutomationJob]:
        jobs: list[AutomationJob] = []
        for item in self._items.values():
            ef = extract_extra_fields(item.get("metadata"))
            state = get_field_value(ef, "Automation state")
            request_fields = RequestFields.from_extra_fields(ef)
            jobs.append(
                AutomationJob(
                    item_id=item["id"],
                    title=item.get("title", ""),
                    state=state,
                    request_fields=request_fields,
                    extra_fields=ef,
                )
            )
        return jobs

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return self._uploads.get(item_id, [])

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        return self._upload_data[(item_id, upload_id)]

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        if item_id not in self._items:
            raise KeyError(f"item {item_id} not found")

        # Simulate transient failures for retry testing
        if self._patch_fail_countdown > 0:
            self._patch_fail_countdown -= 1
            raise ConnectionError("simulated transient eLabFTW failure")

        item = self._items[item_id]
        current_ef = item["metadata"].get("extra_fields") or {}

        # Duplicate-claim guard: if someone is trying to set state=accepted,
        # verify the current state is still "requested".
        new_state_entry = extra_fields.get("Automation state")
        if new_state_entry and isinstance(new_state_entry, dict):
            new_state = new_state_entry.get("value", "")
            current_state = get_field_value(current_ef, "Automation state")
            if new_state == "accepted" and current_state != "requested":
                from bridge.errors import ALREADY_CLAIMED, BridgeError

                raise BridgeError(
                    code=ALREADY_CLAIMED,
                    human_message=(
                        f"Automation Job {item_id} is no longer in 'requested' "
                        f"state (current: {current_state})"
                    ),
                    details={"item_id": item_id, "current_state": current_state},
                )

        # Merge the new fields
        current_ef.update(extra_fields)
        item["metadata"]["extra_fields"] = current_ef

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        upload_id = self._next_upload_id
        self._next_upload_id += 1
        upload = {
            "id": upload_id,
            "real_name": filename,
            "comment": comment,
            "immutable": 0,
            "state": 1,
            "item_id": item_id,
            "filesize": len(content),
        }
        self._uploads.setdefault(item_id, []).append(upload)
        self._upload_data[(item_id, upload_id)] = content
        return upload

    def post_comment(self, item_id: int, comment: str) -> None:
        self._comments.setdefault(item_id, []).append(comment)

    # --- Experiment methods (Stage 6: Assay write-back) ---

    def create_experiment(self, title: str, body: str = "") -> int:
        exp_id = self._next_experiment_id
        self._next_experiment_id += 1
        self._experiments[exp_id] = {"title": title, "body": body, "links": []}
        return exp_id

    def link_experiment_to_item(self, experiment_id: int, item_id: int) -> None:
        if experiment_id not in self._experiments:
            raise KeyError(f"experiment {experiment_id} not found")
        self._experiments[experiment_id]["links"].append(item_id)

    # --- Inspection helpers (for test assertions) ---

    def get_item_state(self, item_id: int) -> str:
        ef = extract_extra_fields(self._items[item_id].get("metadata"))
        return get_field_value(ef, "Automation state")

    def get_item_field(self, item_id: int, field_name: str) -> str:
        ef = extract_extra_fields(self._items[item_id].get("metadata"))
        return get_field_value(ef, field_name)

    def get_item_extra_fields(self, item_id: int) -> dict[str, Any]:
        return extract_extra_fields(self._items[item_id].get("metadata"))

    def get_comments(self, item_id: int) -> list[str]:
        return self._comments.get(item_id, [])

    def get_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return self._uploads.get(item_id, [])

    def get_upload_data(self, item_id: int, upload_id: int) -> bytes:
        return self._upload_data[(item_id, upload_id)]
