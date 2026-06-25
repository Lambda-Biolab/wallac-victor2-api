"""Acceptance criteria tests for issue #2: eLabFTW signature verification
and Automation Job polling/claiming.

AC: "Mock eLabFTW tests cover unsigned job rejection, stale/modified-since-
signature rejection, valid signed job claim, duplicate-claim prevention,
and claim write-back."

These tests use a MockElabftwClient with in-memory state and real Ed25519ph
minisign-compatible signatures generated via PyNaCl, so the signature
verification code is exercised with real crypto.
"""

from __future__ import annotations

import pytest
from bridge_fixtures import (
    MockElabftwClient,
    create_signature_archive,
    generate_minisign_keypair,
    make_data_json,
    make_extra_fields,
)

from bridge.errors import (
    ALREADY_CLAIMED,
    REQUEST_MODIFIED_AFTER_SIGNATURE,
    UNSIGNED_JOB,
    BridgeError,
)
from bridge.intake import JobIntake
from bridge.models import JobState

# --- Shared fixture ---------------------------------------------------------


@pytest.fixture
def setup():
    """Return a bundle of (client, intake, signing_key, pubkey_content)."""
    client = MockElabftwClient()
    signing_key, pubkey_bytes, pubkey_content = generate_minisign_keypair()
    intake = JobIntake(
        client=client,
        bridge_identity="wallac-bridge-test",
        live_monitor_url_base="https://wallac.local:8421",
    )
    return client, intake, signing_key, pubkey_content


def _add_signed_job(
    client: MockElabftwClient,
    signing_key,
    pubkey_content: str,
    item_id: int = 100,
    extra_fields: dict | None = None,
) -> int:
    """Add a signed Automation Job to the mock client."""
    ef = extra_fields or make_extra_fields()
    client.add_item(item_id, extra_fields=ef)
    data_json = make_data_json(item_id, ef)
    archive = create_signature_archive(signing_key, data_json, pubkey_content)
    client.add_signature_upload(item_id, archive)
    return item_id


# --- AC 1: Unsigned job rejection ------------------------------------------


def test_unsigned_job_rejected(setup):
    """A job with no signature archive is rejected with UNSIGNED_JOB."""
    client, intake, _, _ = setup

    # Add a job with no signature
    client.add_item(101, extra_fields=make_extra_fields())

    jobs = intake.find_claimable_jobs()
    assert len(jobs) == 1

    with pytest.raises(BridgeError) as exc_info:
        intake.verify_job_signature(jobs[0])

    assert exc_info.value.code == UNSIGNED_JOB
    assert "no signature archive" in exc_info.value.human_message
    assert exc_info.value.details["item_id"] == 101


# --- AC 2: Stale/modified-since-signature rejection ------------------------


def test_modified_after_signature_rejected(setup):
    """A job whose request fields changed after signing is rejected."""
    client, intake, signing_key, pubkey_content = setup

    # Create and sign a job
    ef = make_extra_fields(protocol_name="Absorbance @ 405")
    _add_signed_job(client, signing_key, pubkey_content, item_id=200, extra_fields=ef)

    # Modify the protocol name AFTER signing (simulates post-signature edit)
    client._items[200]["metadata"]["extra_fields"]["Protocol name"] = {
        "type": "text",
        "value": "Absorbance @ 600",  # changed!
    }

    jobs = intake.find_claimable_jobs()
    assert len(jobs) == 1

    with pytest.raises(BridgeError) as exc_info:
        intake.verify_job_signature(jobs[0])

    assert exc_info.value.code == REQUEST_MODIFIED_AFTER_SIGNATURE
    assert "modified after signature" in exc_info.value.human_message
    # Details should show what changed
    assert (
        exc_info.value.details["signed_fields"]["Protocol name"]
        != exc_info.value.details["current_fields"]["Protocol name"]
    )


# --- AC 3: Valid signed job claim ------------------------------------------


def test_valid_signed_job_claimed(setup):
    """A properly signed, unmodified job is claimed successfully."""
    client, intake, signing_key, pubkey_content = setup

    item_id = _add_signed_job(client, signing_key, pubkey_content, item_id=300)

    jobs = intake.find_claimable_jobs()
    assert len(jobs) == 1

    # Verify signature
    job = intake.verify_job_signature(jobs[0])
    assert job.signature_info is not None
    assert job.signature_info.signer_email == "test@example.org"
    assert job.signature_info.meaning == "Approval"
    assert job.signed_snapshot is not None

    # Claim
    live_url = intake.claim_job(job)
    assert live_url == f"https://wallac.local:8421/jobs/{item_id}"

    # Verify write-back
    assert client.get_item_state(item_id) == JobState.ACCEPTED.value
    assert client.get_item_field(item_id, "Claimed by") == "wallac-bridge-test"
    assert client.get_item_field(item_id, "Claimed at") != ""
    assert client.get_item_field(item_id, "Live Monitor") == live_url


# --- AC 4: Duplicate-claim prevention --------------------------------------


def test_duplicate_claim_prevented(setup):
    """A job already claimed by another instance is not re-claimed."""
    client, intake, signing_key, pubkey_content = setup

    _add_signed_job(client, signing_key, pubkey_content, item_id=400)

    # First instance claims the job
    jobs = intake.find_claimable_jobs()
    job = intake.verify_job_signature(jobs[0])
    intake.claim_job(job)

    # Simulate a second bridge instance trying to claim the same job
    # (it re-reads the job list and finds it's no longer "requested")
    jobs_again = intake.find_claimable_jobs()
    assert len(jobs_again) == 0  # state is now "accepted", not "requested"

    # Even if we bypass find_claimable_jobs and try to claim directly:
    # re-verify the job (which still has the old state in the AutomationJob
    # object), then attempt to claim — should fail with ALREADY_CLAIMED
    job.state = JobState.REQUESTED.value  # simulate stale local state
    with pytest.raises(BridgeError) as exc_info:
        intake.claim_job(job)

    assert exc_info.value.code == ALREADY_CLAIMED
    assert "no longer in 'requested'" in exc_info.value.human_message


# --- AC 5: Claim write-back -------------------------------------------------


def test_claim_write_back_fields(setup):
    """Claiming writes back all required fields to eLabFTW."""
    client, intake, signing_key, pubkey_content = setup

    item_id = _add_signed_job(client, signing_key, pubkey_content, item_id=500)

    # Process the full intake cycle
    results = intake.process_pending()

    assert len(results) == 1
    result = results[0]
    assert result["item_id"] == item_id
    assert result["status"] == "claimed"
    assert result["live_monitor_url"] == f"https://wallac.local:8421/jobs/{item_id}"
    assert result["signer"] == "test@example.org"

    # Verify all write-back fields are present in the mock
    ef = client.get_item_extra_fields(item_id)
    assert ef["Automation state"]["value"] == JobState.ACCEPTED.value
    assert ef["Claimed by"]["value"] == "wallac-bridge-test"
    assert ef["Claimed at"]["value"] != ""
    assert ef["Live Monitor"]["value"] == result["live_monitor_url"]


# --- Bonus: rejection write-back -------------------------------------------


def test_rejection_write_back(setup):
    """A rejected job is written back with operator-review state."""
    client, intake, _, _ = setup

    # Add an unsigned job
    client.add_item(600, extra_fields=make_extra_fields())

    results = intake.process_pending()

    assert len(results) == 1
    result = results[0]
    assert result["status"] == "rejected"
    assert result["error"]["code"] == UNSIGNED_JOB

    # Verify the rejection was written back
    assert client.get_item_state(600) == JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value
    assert client.get_item_field(600, "Last error code") == UNSIGNED_JOB
    assert client.get_item_field(600, "Operator hint") != ""


# --- Bonus: non-matching jobs are filtered ---------------------------------


def test_non_matching_jobs_filtered(setup):
    """Jobs with wrong service, action, or state are not claimable."""
    client, intake, _, _ = setup

    # Wrong service
    client.add_item(1, extra_fields=make_extra_fields(automation_service="other_service"))
    # Wrong action
    client.add_item(2, extra_fields=make_extra_fields(requested_action="abort"))
    # Wrong state (already accepted)
    client.add_item(3, extra_fields=make_extra_fields(automation_state="accepted"))

    jobs = intake.find_claimable_jobs()
    assert len(jobs) == 0
