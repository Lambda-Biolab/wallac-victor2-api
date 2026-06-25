"""Job lifecycle state machine and recovery semantics for the Wallac bridge.

Implements issue #5: controlled abort and recovery behavior.

The state machine governs transitions for a claimed Automation Job, with
particular attention to abort paths:

  running → abort_requested → aborting → aborted | failed

Recovery logic classifies a persisted state after restart, network
interruption, or service error into one of four terminal states:

  completed | failed | aborted | unknown_requires_operator_review

Key rule: **never automatically repeat ambiguous physical work.** If the
bridge cannot determine whether a run completed, it marks the job for
operator review rather than re-executing.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
                 eLabFTW-lambdabiolab/docs/automation-integrations.md
"""

from __future__ import annotations

import enum
import logging

from .errors import BridgeError, Severity
from .models import AutomationJob, FinalState, JobState

logger = logging.getLogger(__name__)


# --- Abort sources ---------------------------------------------------------


class AbortSource(str, enum.Enum):
    """Where an abort request originated.

    ELABFTW aborts are non-real-time operator cancel intent (polled within
    ~5-15 seconds). DASHBOARD aborts are lower-latency direct requests from
    the live dashboard. EMERGENCY_STOP is a separate physical path and is
    NOT handled by this state machine — it bypasses software entirely.
    """

    ELABFTW = "elabftw"
    DASHBOARD = "dashboard"
    EMERGENCY_STOP = "emergency_stop"  # informational only — not processed here


# --- Stable error codes for abort/recovery ---------------------------------

ABORT_TOO_EARLY = "abort_too_early"
ABORT_FAILED = "abort_failed"
AMBIGUOUS_STATE = "ambiguous_state"
RUN_NOT_FOUND = "run_not_found"


# --- State machine ---------------------------------------------------------


# Valid forward transitions.  Any transition not in this map is rejected.
_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.ACCEPTED: frozenset(
        {
            JobState.QUEUED,
            JobState.VALIDATING,
            JobState.ABORTED,  # abort before run starts
            JobState.FAILED,
        }
    ),
    JobState.QUEUED: frozenset(
        {
            JobState.VALIDATING,
            JobState.ABORTED,  # abort before run starts
            JobState.FAILED,
        }
    ),
    JobState.VALIDATING: frozenset(
        {
            JobState.READY,
            JobState.FAILED,
            JobState.ABORTED,  # abort during validation
        }
    ),
    JobState.READY: frozenset(
        {
            JobState.RUNNING,
            JobState.ABORTED,  # abort before carrier moves
            JobState.FAILED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.ABORT_REQUESTED,
            JobState.FAILED,
            JobState.RESULTS_READY,  # run completed normally
        }
    ),
    JobState.ABORT_REQUESTED: frozenset(
        {
            JobState.ABORTING,
            JobState.RESULTS_READY,  # run finished before abort took effect
            JobState.FAILED,
        }
    ),
    JobState.ABORTING: frozenset(
        {
            JobState.ABORTED,
            JobState.FAILED,  # abort itself failed
        }
    ),
    JobState.ABORTED: frozenset({}),  # terminal
    JobState.FAILED: frozenset({}),  # terminal
    JobState.RESULTS_READY: frozenset(
        {
            JobState.RESULTS_UPLOADED,
            JobState.FAILED,  # write-back failed
        }
    ),
    JobState.RESULTS_UPLOADED: frozenset(
        {
            JobState.COMPLETED,
        }
    ),
    JobState.COMPLETED: frozenset({}),  # terminal
    # draft/requested/rejected are pre-claim; not managed here
    JobState.DRAFT: frozenset({JobState.REQUESTED}),
    JobState.REQUESTED: frozenset({JobState.ACCEPTED, JobState.REJECTED}),
    JobState.REJECTED: frozenset({}),
    JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW: frozenset({}),  # terminal
}


# States that are considered "terminal" for recovery purposes.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.ABORTED,
        JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW,
    }
)

# States that are "ambiguous" on restart — we don't know if the physical
# run completed.
AMBIGUOUS_STATES: frozenset[JobState] = frozenset(
    {
        JobState.RUNNING,
        JobState.ABORT_REQUESTED,
        JobState.ABORTING,
        JobState.QUEUED,
        JobState.VALIDATING,
        JobState.READY,
    }
)


class LifecycleManager:
    """State machine for a single claimed Automation Job.

    Tracks the job's state and enforces valid transitions.  Handles abort
    requests from eLabFTW polling or the dashboard, and provides the context
    needed for recovery classification.

    This is pure logic — no I/O, no eLabFTW calls, no instrument calls.
    The caller (intake/execution loop) drives state transitions based on
    real events.
    """

    def __init__(self, job: AutomationJob) -> None:
        self.job = job
        # Start from the job's current eLabFTW state (after claim = accepted)
        try:
            self._state = JobState(job.state)
        except ValueError:
            self._state = JobState.ACCEPTED
        self._abort_source: AbortSource | None = None
        self._last_error: BridgeError | None = None

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def is_running(self) -> bool:
        return self._state == JobState.RUNNING

    @property
    def is_abort_requested(self) -> bool:
        return self._state == JobState.ABORT_REQUESTED

    @property
    def abort_source(self) -> AbortSource | None:
        return self._abort_source

    @property
    def last_error(self) -> BridgeError | None:
        return self._last_error

    def _transition(self, new_state: JobState) -> None:
        """Enforce a valid state transition or raise BridgeError."""
        allowed = _TRANSITIONS.get(self._state, frozenset())
        if new_state not in allowed:
            raise BridgeError(
                code="invalid_transition",
                severity=Severity.ERROR,
                human_message=(
                    f"Invalid state transition: {self._state.value} → {new_state.value}"
                ),
                operator_hint="This indicates a bug in the bridge state machine.",
                details={
                    "from_state": self._state.value,
                    "to_state": new_state.value,
                    "item_id": self.job.item_id,
                },
            )
        old = self._state
        self._state = new_state
        logger.debug("Job %d: %s → %s", self.job.item_id, old.value, new_state.value)

    # --- Normal lifecycle --------------------------------------------------

    def start_validation(self) -> None:
        """Transition from accepted/queued to validating."""
        if self._state == JobState.ACCEPTED:
            self._transition(JobState.QUEUED)
        self._transition(JobState.VALIDATING)

    def validation_passed(self) -> None:
        """Transition from validating to ready."""
        self._transition(JobState.READY)

    def start_run(self) -> None:
        """Transition from ready to running (carrier begins moving)."""
        self._transition(JobState.RUNNING)

    def run_completed(self) -> None:
        """Transition from running to results_ready (measurement finished)."""
        self._transition(JobState.RESULTS_READY)

    def results_uploaded(self) -> None:
        """Transition from results_ready to results_uploaded."""
        self._transition(JobState.RESULTS_UPLOADED)

    def complete(self) -> None:
        """Transition from results_uploaded to completed."""
        self._transition(JobState.COMPLETED)

    def fail(self, error: BridgeError) -> None:
        """Transition to failed state with an error."""
        self._last_error = error
        self._transition(JobState.FAILED)

    # --- Abort lifecycle ---------------------------------------------------

    def request_abort(self, source: AbortSource) -> None:
        """Request a controlled (non-emergency) abort.

        - If the job hasn't started running yet → go directly to ABORTED
          (no physical work was done).
        - If the job is running → go to ABORT_REQUESTED (the execution
          loop will pick this up and call begin_aborting()).
        - If the job is already terminal → no-op (abort after completion).
        - If the job is already aborting → no-op.

        Emergency stops (source=EMERGENCY_STOP) are NOT processed here —
        they bypass the software state machine entirely.
        """
        if source == AbortSource.EMERGENCY_STOP:
            # Emergency stop is a separate physical path.  We log it but do
            # not drive a state transition — the operator must physically
            # intervene and then mark the job for review.
            logger.warning(
                "Job %d: emergency stop signaled (physical path, not processed by state machine)",
                self.job.item_id,
            )
            self._abort_source = source
            return

        # Abort after completion — no-op
        if self._state in TERMINAL_STATES:
            logger.info(
                "Job %d: abort requested but job is already terminal (%s)",
                self.job.item_id,
                self._state.value,
            )
            return

        # Abort before run starts — go directly to aborted
        if self._state in (JobState.ACCEPTED, JobState.QUEUED, JobState.VALIDATING, JobState.READY):
            self._abort_source = source
            self._transition(JobState.ABORTED)
            logger.info(
                "Job %d: aborted before run started (source=%s)",
                self.job.item_id,
                source.value,
            )
            return

        # Abort during run — request abort
        if self._state == JobState.RUNNING:
            self._abort_source = source
            self._transition(JobState.ABORT_REQUESTED)
            logger.info(
                "Job %d: abort requested (source=%s)",
                self.job.item_id,
                source.value,
            )
            return

        # Already aborting — no-op
        if self._state in (JobState.ABORT_REQUESTED, JobState.ABORTING):
            logger.info(
                "Job %d: abort already in progress (%s)",
                self.job.item_id,
                self._state.value,
            )
            return

    def begin_aborting(self) -> None:
        """Transition from abort_requested to aborting.

        Called by the execution loop when it picks up the abort request and
        starts the controlled software abort (e.g., calls POST /runs/{id}/abort
        on the vm-agent).
        """
        self._transition(JobState.ABORTING)

    def abort_succeeded(self) -> None:
        """Transition from aborting to aborted."""
        self._transition(JobState.ABORTED)

    def abort_failed(self, error: BridgeError) -> None:
        """Transition from aborting to failed (the abort itself failed)."""
        self._last_error = error
        self._transition(JobState.FAILED)

    def run_finished_before_abort(self) -> None:
        """The run completed before the abort could take effect.

        Transition from abort_requested to results_ready.
        """
        self._transition(JobState.RESULTS_READY)

    # --- Error reporting ---------------------------------------------------

    def to_error(self) -> BridgeError:
        """Return a structured error describing the current state.

        Used when writing back to eLabFTW or surfacing to the operator.
        """
        if self._last_error is not None:
            return self._last_error
        if self._state == JobState.ABORTED:
            return BridgeError(
                code="aborted",
                severity=Severity.INFO,
                human_message=f"Job {self.job.item_id} was aborted",
                operator_hint="The run was cancelled. Re-sign a new Automation Job to retry.",
                details={
                    "item_id": self.job.item_id,
                    "abort_source": self._abort_source.value if self._abort_source else None,
                },
            )
        if self._state == JobState.UNKNOWN_REQUIRES_OPERATOR_REVIEW:
            return BridgeError(
                code=AMBIGUOUS_STATE,
                severity=Severity.WARNING,
                human_message=(f"Job {self.job.item_id} requires operator review"),
                operator_hint=(
                    "The bridge could not determine the final state of this "
                    "run. Inspect the instrument and results manually before "
                    "proceeding."
                ),
                details={"item_id": self.job.item_id},
            )
        return BridgeError(
            code="unknown",
            human_message=f"Job {self.job.item_id} is in state {self._state.value}",
            details={"item_id": self.job.item_id, "state": self._state.value},
        )


# --- Recovery ---------------------------------------------------------------


class RecoveryManager:
    """Classifies a persisted job state after restart or interruption.

    On restart, network interruption, service error, or operator abort,
    the bridge must report one of: completed, failed, aborted, or
    unknown_requires_operator_review.

    Key rule: **never automatically repeat ambiguous physical work.** If
    the persisted state is ambiguous (running, aborting, etc.), the job
    is marked for operator review — not re-executed.
    """

    @staticmethod
    def classify(persisted_state: JobState, *, has_results: bool = False) -> FinalState:
        """Classify a persisted state into a terminal final state.

        Args:
            persisted_state: The last known state from local persistence
                             or eLabFTW.
            has_results: Whether result artifacts exist locally (e.g.,
                         a completed run's data was persisted before the
                         crash).

        Returns:
            One of the four terminal FinalState values.
        """
        # Already terminal — report as-is
        if persisted_state == JobState.COMPLETED:
            return FinalState.COMPLETED
        if persisted_state == JobState.FAILED:
            return FinalState.FAILED
        if persisted_state == JobState.ABORTED:
            return FinalState.ABORTED

        # Results were persisted → the run completed, just write-back didn't
        if has_results and persisted_state in (
            JobState.RESULTS_READY,
            JobState.RESULTS_UPLOADED,
        ):
            return FinalState.COMPLETED

        # Results-ready/uploaded but no local results → ambiguous
        if persisted_state in (JobState.RESULTS_READY, JobState.RESULTS_UPLOADED):
            return FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW

        # Any active/ambiguous state → operator review (never auto-repeat)
        if persisted_state in AMBIGUOUS_STATES:
            return FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW

        # Unknown state → operator review
        return FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW

    @staticmethod
    def classify_with_error(
        persisted_state: JobState, *, has_results: bool = False
    ) -> tuple[FinalState, BridgeError]:
        """Classify and return a structured error for the operator."""
        final = RecoveryManager.classify(persisted_state, has_results=has_results)

        if final == FinalState.UNKNOWN_REQUIRES_OPERATOR_REVIEW:
            error = BridgeError(
                code=AMBIGUOUS_STATE,
                severity=Severity.WARNING,
                human_message=(
                    f"Job state '{persisted_state.value}' is ambiguous after "
                    f"restart. The bridge will not automatically re-execute."
                ),
                operator_hint=(
                    "Inspect the instrument and any local result artifacts. "
                    "If the run completed, mark the job as completed. If not, "
                    "create a new signed Automation Job to retry."
                ),
                retryable=False,
                details={
                    "persisted_state": persisted_state.value,
                    "has_results": has_results,
                },
            )
            return final, error

        if final == FinalState.COMPLETED:
            error = BridgeError(
                code="completed",
                severity=Severity.INFO,
                human_message="Job completed successfully",
                operator_hint="No action needed.",
                details={"persisted_state": persisted_state.value},
            )
        elif final == FinalState.FAILED:
            error = BridgeError(
                code="failed",
                severity=Severity.ERROR,
                human_message="Job failed",
                operator_hint="Review the error log and create a new signed job to retry.",
                details={"persisted_state": persisted_state.value},
            )
        elif final == FinalState.ABORTED:
            error = BridgeError(
                code="aborted",
                severity=Severity.INFO,
                human_message="Job was aborted",
                operator_hint="Create a new signed Automation Job to retry.",
                details={"persisted_state": persisted_state.value},
            )

        return final, error
