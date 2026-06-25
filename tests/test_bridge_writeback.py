"""Acceptance criteria tests for issue #4: eLabFTW write-back for status,
progress, and results.

AC: "Mock eLabFTW tests cover normal completion, partial write-back retry,
failed run result package, aborted run result package, and
unknown_requires_operator_review."

AC: "Write-back is append-only where possible and does not mutate signed
request fields."

AC: "Terminal result package includes checksums where practical."
"""

from __future__ import annotations

import pytest
from bridge_fixtures import MockElabftwClient, make_extra_fields

from bridge.models import FinalState, JobState
from bridge.writeback import (
    ArtifactEntry,
    ProgressTracker,
    ResultPackage,
    WriteBackManager,
    compute_checksum,
)

# --- Shared fixture ---------------------------------------------------------


@pytest.fixture
def setup():
    """Return (client, writeback, item_id)."""
    client = MockElabftwClient()
    client.add_item(1, extra_fields=make_extra_fields(automation_state="accepted"))
    writeback = WriteBackManager(
        client=client,
        bridge_identity="wallac-bridge-test",
        device_identity="victor2-serial-4200123",
        max_retries=3,
        retry_delay_seconds=0.01,  # fast for tests
    )
    return client, writeback, 1


# --- AC 1: Normal completion -----------------------------------------------


def test_normal_completion(setup):
    """A successfully completed job writes back all terminal fields + artifacts."""
    client, writeback, item_id = setup

    # Simulate the run lifecycle
    writeback.write_progress(item_id, 0, "starting", "running")
    writeback.write_progress(item_id, 50, "measuring", "running")
    writeback.write_progress(item_id, 100, "done", "results_ready", force=True)

    # Build the terminal result package
    csv_data = b"well,od\nA01,0.071\n"
    package = ResultPackage(
        job_id=item_id,
        experiment_id="42",
        protocol_name="Absorbance @ 405",
        final_state=FinalState.COMPLETED,
        result_summary="8 wells measured, all within acceptance criteria",
        event_log=["Run started", "Measurement complete", "Results uploaded"],
        artifacts=[
            ArtifactEntry(filename="results.csv", content=csv_data, comment="Per-well results"),
        ],
    )

    writeback.write_terminal(item_id, package)

    # Verify metadata write-back
    ef = client.get_item_extra_fields(item_id)
    assert ef["Final state"]["value"] == "completed"
    assert ef["Automation state"]["value"] == JobState.COMPLETED.value
    assert ef["Progress percent"]["value"] == 100.0
    assert ef["Result summary"]["value"] == "8 wells measured, all within acceptance criteria"
    assert ef["Last error code"]["value"] == ""  # no errors

    # Verify artifact was uploaded
    uploads = client.get_uploads(item_id)
    result_uploads = [u for u in uploads if u["real_name"] == "results.csv"]
    assert len(result_uploads) == 1
    assert client.get_upload_data(item_id, result_uploads[0]["id"]) == csv_data

    # Verify event log was appended as comments
    comments = client.get_comments(item_id)
    assert len(comments) == 3
    assert "Run started" in comments[0]
    assert "Results uploaded" in comments[2]


# --- AC 2: Partial write-back retry ----------------------------------------


def test_partial_writeback_retry(setup):
    """Write-back retries after transient eLabFTW failures."""
    client, writeback, item_id = setup

    # Configure the mock to fail the first 2 patch_metadata calls
    client.set_patch_fail_countdown(2)

    # This should retry and eventually succeed
    writeback.write_progress(item_id, 50, "measuring", "running")

    # Verify the write eventually succeeded
    ef = client.get_item_extra_fields(item_id)
    assert ef["Progress percent"]["value"] == 50.0
    assert ef["Current step"]["value"] == "measuring"


def test_writeback_retry_exhausted(setup):
    """Write-back raises after max retries are exhausted."""
    client, writeback, item_id = setup

    # Configure more failures than max_retries
    client.set_patch_fail_countdown(10)

    with pytest.raises(ConnectionError):
        writeback.write_progress(item_id, 50, "measuring", "running")


# --- AC 3: Failed run result package ----------------------------------------


def test_failed_run_result_package(setup):
    """A failed run writes back error fields + final state=failed."""
    client, writeback, item_id = setup

    package = ResultPackage(
        job_id=item_id,
        final_state=FinalState.FAILED,
        result_summary="Instrument returned an error during measurement",
        event_log=["Run started", "COM error: instrument not responding"],
        errors=[
            {
                "code": "instrument_not_connected",
                "severity": "error",
                "human_message": "The instrument lost its COM connection",
                "operator_hint": "Check the ARCnet cable and restart MlrMgr",
                "retryable": True,
            }
        ],
        operator_hint="Check the ARCnet cable and restart MlrMgr, then create a new job",
    )

    writeback.write_terminal(item_id, package)

    ef = client.get_item_extra_fields(item_id)
    assert ef["Final state"]["value"] == "failed"
    assert ef["Automation state"]["value"] == JobState.FAILED.value
    assert ef["Last error code"]["value"] == "instrument_not_connected"
    assert (
        ef["Operator hint"]["value"]
        == "Check the ARCnet cable and restart MlrMgr, then create a new job"
    )
    assert ef["Progress percent"]["value"] == 0.0  # failed = 0%


# --- AC 4: Aborted run result package ---------------------------------------


def test_aborted_run_result_package(setup):
    """An aborted run writes back final state=aborted."""
    client, writeback, item_id = setup

    package = ResultPackage(
        job_id=item_id,
        final_state=FinalState.ABORTED,
        result_summary="Run was aborted by operator request",
        event_log=["Run started", "Abort requested from dashboard", "Abort succeeded"],
        operator_hint="Create a new signed Automation Job to retry",
    )

    writeback.write_terminal(item_id, package)

    ef = client.get_item_extra_fields(item_id)
    assert ef["Final state"]["value"] == "aborted"
    assert ef["Automation state"]["value"] == JobState.ABORTED.value
    assert ef["Progress percent"]["value"] == 0.0

    comments = client.get_comments(item_id)
    assert len(comments) == 3
    assert "Abort succeeded" in comments[2]


# --- AC 5: unknown_requires_operator_review --------------------------------


def test_unknown_requires_operator_review(setup):
    """An ambiguous state writes back unknown_requires_operator_review."""
    client, writeback, item_id = setup

    package = ResultPackage(
        job_id=item_id,
        final_state=FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW,
        result_summary="Bridge restarted during run; final state could not be determined",
        event_log=["Run started", "Bridge process crashed", "Restarted — state is ambiguous"],
        errors=[
            {
                "code": "ambiguous_state",
                "severity": "warning",
                "human_message": "Job state 'running' is ambiguous after restart",
                "operator_hint": "Inspect the instrument and results manually",
                "retryable": False,
            }
        ],
        operator_hint="Inspect the instrument and results manually before creating a new job",
    )

    writeback.write_terminal(item_id, package)

    ef = client.get_item_extra_fields(item_id)
    assert ef["Final state"]["value"] == "unknown_requires_operator_review"
    assert ef["Automation state"]["value"] == JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW.value
    assert ef["Last error code"]["value"] == "ambiguous_state"
    assert "Inspect the instrument" in ef["Operator hint"]["value"]


# --- AC: Write-back does not mutate signed request fields --------------------


def test_writeback_does_not_mutate_request_fields(setup):
    """Write-back never changes signed request fields."""
    client, writeback, item_id = setup

    # Record the original request field values
    original_ef = client.get_item_extra_fields(item_id)
    original_protocol = original_ef["Protocol name"]["value"]
    original_action = original_ef["Requested action"]["value"]
    original_service = original_ef["Automation service"]["value"]

    # Perform various write-backs
    writeback.write_progress(item_id, 50, "measuring", "running")
    writeback.write_heartbeat(item_id)
    writeback.write_terminal(
        item_id,
        ResultPackage(
            job_id=item_id,
            final_state=FinalState.COMPLETED,
            result_summary="Done",
        ),
    )

    # Verify request fields are unchanged
    final_ef = client.get_item_extra_fields(item_id)
    assert final_ef["Protocol name"]["value"] == original_protocol
    assert final_ef["Requested action"]["value"] == original_action
    assert final_ef["Automation service"]["value"] == original_service


# --- AC: Terminal result package includes checksums -------------------------


def test_artifact_checksums_computed(setup):
    """Artifacts include SHA-256 checksums."""
    client, writeback, item_id = setup

    csv_data = b"well,od\nA01,0.071\n"
    package = ResultPackage(
        job_id=item_id,
        final_state=FinalState.COMPLETED,
        artifacts=[
            ArtifactEntry(filename="results.csv", content=csv_data),
        ],
    )

    writeback.write_terminal(item_id, package)

    # Verify checksum was computed
    artifact = package.artifacts[0]
    assert artifact.checksum == compute_checksum(csv_data)
    assert len(artifact.checksum) == 64  # SHA-256 hex

    # Verify checksum is in the artifact manifest
    import json

    ef = client.get_item_extra_fields(item_id)
    manifest = json.loads(ef["Artifact manifest"]["value"])
    assert manifest["artifacts"][0]["checksum"] == artifact.checksum
    assert manifest["artifacts"][0]["filename"] == "results.csv"
    assert manifest["artifacts"][0]["size"] == len(csv_data)


# --- Bonus: Progress throttling ---------------------------------------------


def test_progress_throttling():
    """ProgressTracker only writes when thresholds are met."""
    tracker = ProgressTracker(interval_seconds=100, delta_percent=5.0)

    # First write always goes through
    assert tracker.should_write(0, "running") is True
    tracker.mark_written(0, "running")

    # Same percent + state → throttled
    assert tracker.should_write(0, "running") is False

    # Small delta → throttled
    assert tracker.should_write(2, "running") is False

    # Large enough delta → write
    assert tracker.should_write(6, "running") is True
    tracker.mark_written(6, "running")

    # State change → always write
    assert tracker.should_write(6, "results_ready") is True
    tracker.mark_written(6, "results_ready")

    # Force → always write
    assert tracker.should_write(6, "results_ready", force=True) is True


# --- Bonus: Claim fields write-back -----------------------------------------


def test_claim_fields_writeback(setup):
    """write_claim writes all claim fields."""
    client, writeback, item_id = setup

    writeback.write_claim(
        item_id,
        wallac_run_id="r-ab12cd34",
        live_monitor_url="https://wallac.local:8421/jobs/1",
    )

    ef = client.get_item_extra_fields(item_id)
    assert ef["Claimed by"]["value"] == "wallac-bridge-test"
    assert ef["Claimed at"]["value"] != ""
    assert ef["Last heartbeat"]["value"] != ""
    assert ef["Wallac run ID"]["value"] == "r-ab12cd34"
    assert ef["Device identity"]["value"] == "victor2-serial-4200123"
    assert ef["Live Monitor"]["value"] == "https://wallac.local:8421/jobs/1"


# --- Bonus: Event log append-only -------------------------------------------


def test_event_log_append_only(setup):
    """Events are appended as comments, not overwritten."""
    client, writeback, item_id = setup

    writeback.write_event(item_id, "Run started")
    writeback.write_event(item_id, "Measurement in progress")
    writeback.write_event(item_id, "Measurement complete")

    comments = client.get_comments(item_id)
    assert len(comments) == 3
    assert "Run started" in comments[0]
    assert "Measurement in progress" in comments[1]
    assert "Measurement complete" in comments[2]
