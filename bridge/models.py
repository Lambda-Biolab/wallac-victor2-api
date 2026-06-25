"""Data models for the Wallac eLabFTW bridge.

Maps the Automation Job resource schema defined in
docs/wallac-plate-reader-integration.md (eLabFTW-lambdabiolab repo) to Python
dataclasses.  The bridge reads operator-owned request fields from signed
Automation Jobs and writes bridge-owned state/progress/result/error fields
back via the eLabFTW API.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# --- Job lifecycle states (automation-integrations.md "Job states") --------


class JobState(str, enum.Enum):
    """Lifecycle states for an Automation Job.

    Services may add intermediate states; these are the minimal shared states
    from the automation-integrations standard.
    """

    DRAFT = "draft"
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUEUED = "queued"
    VALIDATING = "validating"
    READY = "ready"
    RUNNING = "running"
    ABORT_REQUESTED = "abort_requested"
    ABORTING = "aborting"
    ABORTED = "aborted"
    FAILED = "failed"
    RESULTS_READY = "results_ready"
    RESULTS_UPLOADED = "results_uploaded"
    COMPLETED = "completed"
    UNKNOWN_REQUIRES_OPERATOR_REVIEW = "unknown_requires_operator_review"


class FinalState(str, enum.Enum):
    """Terminal states reported after restart, abort, or completion."""

    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    UNKNOWN_REQUIRES_OPERATOR_REVIEW = "unknown_requires_operator_review"


class RequestedAction(str, enum.Enum):
    NONE = "none"
    SUBMIT = "submit"
    ABORT = "abort"


# Operator-owned request fields — immutable after signature.
# (wallac-plate-reader-integration.md "ELN objects" table, Request group)
REQUEST_FIELD_NAMES: tuple[str, ...] = (
    "Automation service",
    "Linked experiment ID",
    "Protocol name",
    "Plate layout reference",
    "Expected outputs",
    "Requested action",
)


@dataclass
class RequestFields:
    """Snapshot of the operator-owned request fields from an Automation Job.

    These fields are immutable after the job is signed.  The bridge snapshots
    them before execution and uses the snapshot to detect post-signature
    modifications.
    """

    automation_service: str = ""
    linked_experiment_id: str = ""
    protocol_name: str = ""
    plate_layout_reference: str = ""
    expected_outputs: str = ""
    requested_action: str = ""

    @classmethod
    def from_extra_fields(cls, extra_fields: dict[str, Any]) -> RequestFields:
        """Extract request fields from eLabFTW extra_fields metadata."""

        def _val(name: str) -> str:
            entry = extra_fields.get(name)
            if isinstance(entry, dict):
                return str(entry.get("value", ""))
            if entry is None:
                return ""
            return str(entry)

        return cls(
            automation_service=_val("Automation service"),
            linked_experiment_id=_val("Linked experiment ID"),
            protocol_name=_val("Protocol name"),
            plate_layout_reference=_val("Plate layout reference"),
            expected_outputs=_val("Expected outputs"),
            requested_action=_val("Requested action"),
        )

    def to_dict(self) -> dict[str, str]:
        """Return as a dict keyed by eLabFTW field name for comparison."""
        return {
            "Automation service": self.automation_service,
            "Linked experiment ID": self.linked_experiment_id,
            "Protocol name": self.protocol_name,
            "Plate layout reference": self.plate_layout_reference,
            "Expected outputs": self.expected_outputs,
            "Requested action": self.requested_action,
        }


@dataclass
class SignatureInfo:
    """Metadata extracted from a verified eLabFTW minisign signature.

    eLabFTW signs entities with Ed25519ph (pre-hashed) minisign-compatible
    signatures.  The trusted comment contains signer identity, timestamp,
    and meaning.
    """

    signer_firstname: str
    signer_lastname: str
    signer_email: str
    signed_at: str  # ISO 8601
    meaning: str  # Approval, Authorship, Responsibility, Review, Safety
    key_id: str  # hex key ID


@dataclass
class AutomationJob:
    """An eLabFTW Automation Job resource (item in the Automation Job category).

    Maps to an eLabFTW ``items`` row whose category is the Automation Job
    resource category (id=9 in the lambdabiolab deployment).
    """

    item_id: int
    title: str
    state: str  # JobState value
    request_fields: RequestFields
    extra_fields: dict[str, Any] = field(default_factory=dict)
    signature_info: SignatureInfo | None = None
    signed_snapshot: dict[str, Any] | None = None  # data.json from signature archive
