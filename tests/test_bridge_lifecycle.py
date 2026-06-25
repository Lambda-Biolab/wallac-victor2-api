"""Acceptance criteria tests for issue #5: controlled abort and recovery
semantics.

AC: "Tests cover abort before run starts, abort during run, abort after
completion, service restart with known completed state, and restart with
ambiguous state."

These tests exercise the pure state machine and recovery logic — no I/O,
no instrument, no eLabFTW calls.
"""

from __future__ import annotations

import pytest
from bridge_fixtures import MockElabftwClient, make_extra_fields

from bridge.errors import BridgeError, Severity
from bridge.lifecycle import (
    AMBIGUOUS_STATE,
    AbortSource,
    LifecycleManager,
    RecoveryManager,
)
from bridge.models import AutomationJob, FinalState, JobState, RequestFields

# --- Helpers ----------------------------------------------------------------


def make_job(item_id: int = 1, state: str = "accepted") -> AutomationJob:
    """Create an AutomationJob in the given state."""
    return AutomationJob(
        item_id=item_id,
        title="Test Job",
        state=state,
        request_fields=RequestFields(),
        extra_fields=make_extra_fields(automation_state=state),
    )


def make_lifecycle(state: str = "accepted") -> LifecycleManager:
    """Create a LifecycleManager for a job in the given state."""
    return LifecycleManager(make_job(state=state))


# --- AC 1: Abort before run starts -----------------------------------------


def test_abort_before_run_starts():
    """An abort requested before the run starts goes directly to ABORTED.

    No physical work was done, so no ambiguous state.
    """
    lifecycle = make_lifecycle("accepted")

    # Abort from eLabFTW (polled)
    lifecycle.request_abort(AbortSource.ELABFTW)

    assert lifecycle.state == JobState.ABORTED
    assert lifecycle.is_terminal
    assert lifecycle.abort_source == AbortSource.ELABFTW

    # The error should be informational, not an error
    error = lifecycle.to_error()
    assert error.severity == Severity.INFO
    assert error.code == "aborted"


def test_abort_during_validation():
    """Abort during validation (before carrier moves) also goes to ABORTED."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()

    # Abort from dashboard (lower latency)
    lifecycle.request_abort(AbortSource.DASHBOARD)

    assert lifecycle.state == JobState.ABORTED
    assert lifecycle.abort_source == AbortSource.DASHBOARD


# --- AC 2: Abort during run ------------------------------------------------


def test_abort_during_run():
    """An abort requested during a running job goes through the full
    abort lifecycle: running → abort_requested → aborting → aborted."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()
    assert lifecycle.is_running

    # Abort detected from eLabFTW polling
    lifecycle.request_abort(AbortSource.ELABFTW)
    assert lifecycle.state == JobState.ABORT_REQUESTED
    assert lifecycle.is_abort_requested

    # Execution loop picks up the abort and begins aborting
    lifecycle.begin_aborting()
    assert lifecycle.state == JobState.ABORTING

    # Controlled abort succeeds
    lifecycle.abort_succeeded()
    assert lifecycle.state == JobState.ABORTED
    assert lifecycle.is_terminal


def test_abort_during_run_from_dashboard():
    """Dashboard abort also triggers the abort lifecycle."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    lifecycle.request_abort(AbortSource.DASHBOARD)
    assert lifecycle.state == JobState.ABORT_REQUESTED

    lifecycle.begin_aborting()
    lifecycle.abort_succeeded()
    assert lifecycle.state == JobState.ABORTED


def test_abort_during_run_fails():
    """If the controlled abort itself fails, the job goes to FAILED."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    lifecycle.request_abort(AbortSource.ELABFTW)
    lifecycle.begin_aborting()

    abort_error = BridgeError(
        code="abort_failed",
        human_message="The instrument did not respond to the abort command",
    )
    lifecycle.abort_failed(abort_error)

    assert lifecycle.state == JobState.FAILED
    assert lifecycle.is_terminal
    assert lifecycle.last_error is not None


def test_run_finished_before_abort_takes_effect():
    """If the run completes before the abort is processed, it's not aborted."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    lifecycle.request_abort(AbortSource.ELABFTW)
    assert lifecycle.state == JobState.ABORT_REQUESTED

    # The run finished before we could abort
    lifecycle.run_finished_before_abort()
    assert lifecycle.state == JobState.RESULTS_READY

    # Continue to completion
    lifecycle.results_uploaded()
    lifecycle.complete()
    assert lifecycle.state == JobState.COMPLETED


# --- AC 3: Abort after completion ------------------------------------------


def test_abort_after_completion():
    """An abort requested after the job is already complete is a no-op."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()
    lifecycle.run_completed()
    lifecycle.results_uploaded()
    lifecycle.complete()
    assert lifecycle.state == JobState.COMPLETED

    # Abort request arrives late — should be a no-op
    lifecycle.request_abort(AbortSource.ELABFTW)
    assert lifecycle.state == JobState.COMPLETED  # unchanged


def test_abort_after_already_aborted():
    """A second abort request on an already-aborted job is a no-op."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.request_abort(AbortSource.ELABFTW)
    assert lifecycle.state == JobState.ABORTED

    # Second abort — no-op
    lifecycle.request_abort(AbortSource.DASHBOARD)
    assert lifecycle.state == JobState.ABORTED


# --- AC 4: Restart with known completed state ------------------------------


def test_restart_known_completed_state():
    """A restart with a persisted 'completed' state reports COMPLETED."""
    final = RecoveryManager.classify(JobState.COMPLETED)
    assert final == FinalState.COMPLETED

    final, error = RecoveryManager.classify_with_error(JobState.COMPLETED)
    assert final == FinalState.COMPLETED
    assert error.severity == Severity.INFO


def test_restart_known_failed_state():
    """A restart with a persisted 'failed' state reports FAILED."""
    final = RecoveryManager.classify(JobState.FAILED)
    assert final == FinalState.FAILED


def test_restart_known_aborted_state():
    """A restart with a persisted 'aborted' state reports ABORTED."""
    final = RecoveryManager.classify(JobState.ABORTED)
    assert final == FinalState.ABORTED


def test_restart_results_ready_with_local_results():
    """A restart with results_ready + local result artifacts → COMPLETED."""
    final = RecoveryManager.classify(JobState.RESULTS_READY, has_results=True)
    assert final == FinalState.COMPLETED

    final = RecoveryManager.classify(JobState.RESULTS_UPLOADED, has_results=True)
    assert final == FinalState.COMPLETED


# --- AC 5: Restart with ambiguous state ------------------------------------


def test_restart_ambiguous_running_state():
    """A restart with a persisted 'running' state → operator review.

    The bridge never auto-repeats ambiguous physical work.
    """
    final = RecoveryManager.classify(JobState.RUNNING)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW

    final, error = RecoveryManager.classify_with_error(JobState.RUNNING)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW
    assert error.code == AMBIGUOUS_STATE
    assert error.severity == Severity.WARNING
    assert "not automatically re-execute" in error.human_message
    assert error.details["persisted_state"] == "running"


def test_restart_ambiguous_aborting_state():
    """A restart with a persisted 'aborting' state → operator review."""
    final = RecoveryManager.classify(JobState.ABORTING)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW


def test_restart_ambiguous_abort_requested_state():
    """A restart with a persisted 'abort_requested' state → operator review."""
    final = RecoveryManager.classify(JobState.ABORT_REQUESTED)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW


def test_restart_results_ready_without_local_results():
    """A restart with results_ready but no local results → operator review."""
    final = RecoveryManager.classify(JobState.RESULTS_READY, has_results=False)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW


def test_restart_validating_state():
    """A restart with a persisted 'validating' state → operator review."""
    final = RecoveryManager.classify(JobState.VALIDATING)
    assert final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW


# --- Bonus: invalid transitions are rejected -------------------------------


def test_invalid_transition_rejected():
    """Cannot skip states (e.g., accepted → completed without running)."""
    lifecycle = make_lifecycle("accepted")

    with pytest.raises(BridgeError) as exc_info:
        lifecycle.complete()  # can't go accepted → completed

    assert exc_info.value.code == "invalid_transition"
    assert "accepted" in exc_info.value.human_message
    assert "completed" in exc_info.value.human_message


def test_emergency_stop_not_processed():
    """Emergency stop is logged but does not drive a state transition."""
    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    lifecycle.request_abort(AbortSource.EMERGENCY_STOP)

    # State is unchanged — emergency stop is a physical path
    assert lifecycle.state == JobState.RUNNING
    assert lifecycle.abort_source == AbortSource.EMERGENCY_STOP


# --- Bonus: abort detector with mock eLabFTW -------------------------------


def test_abort_detector_detects_elabftw_abort():
    """The AbortDetector detects action=abort on a running job."""
    from bridge.abort import AbortDetector

    client = MockElabftwClient()
    # Add a job in "accepted" state with action=submit
    client.add_item(
        1, extra_fields=make_extra_fields(automation_state="accepted", requested_action="submit")
    )

    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    detector = AbortDetector(client)

    # No abort yet
    results = detector.check_for_aborts([(1, lifecycle)])
    assert len(results) == 0
    assert lifecycle.state == JobState.RUNNING

    # Operator changes action to abort in eLabFTW
    client._items[1]["metadata"]["extra_fields"]["Requested action"] = {
        "type": "select",
        "value": "abort",
    }

    # Next poll detects it
    results = detector.check_for_aborts([(1, lifecycle)])
    assert len(results) == 1
    assert results[0]["abort_source"] == "elabftw"
    assert lifecycle.state == JobState.ABORT_REQUESTED


def test_dashboard_abort_handler():
    """The DashboardAbortHandler provides lower-latency abort."""
    from bridge.abort import DashboardAbortHandler

    lifecycle = make_lifecycle("accepted")
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()

    handler = DashboardAbortHandler()
    handler.register(1, lifecycle)

    result = handler.request_abort(1)
    assert result["abort_source"] == "dashboard"
    assert result["old_state"] == "running"
    assert result["new_state"] == "abort_requested"
    assert lifecycle.state == JobState.ABORT_REQUESTED

    # Cleanup after terminal
    handler.unregister(1)
    assert not handler.is_registered(1)
