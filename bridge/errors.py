"""Structured error types for the Wallac eLabFTW bridge.

Follows the error shape defined in docs/automation-integrations.md
"Error shape": code, severity, human_message, operator_hint, retryable, details.
"""

from __future__ import annotations

import enum
from typing import Any


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


# --- Stable error codes for intake/claim failures --------------------------

UNSIGNED_JOB = "unsigned_job"
SIGNATURE_VERIFICATION_FAILED = "signature_verification_failed"
REQUEST_MODIFIED_AFTER_SIGNATURE = "request_modified_after_signature"
ALREADY_CLAIMED = "already_claimed"
CLAIM_FAILED = "claim_failed"
ELABFTW_ERROR = "elabftw_error"

# --- Stable error codes for canonical spec / generated-authoring failures ---
# Source: docs/plans/wallac-protocol-authoring.md "Error taxonomy"

CANONICAL_HASH_MISMATCH = "canonical_hash_mismatch"
CANONICAL_ATTACHMENT_MISMATCH = "canonical_attachment_mismatch"
SCHEMA_UNSUPPORTED = "schema_unsupported"
SIGNATURE_MISSING = "signature_missing"
SIGNATURE_INVALID = "signature_invalid"
SIGNER_UNAUTHORIZED = "signer_unauthorized"
REFERENCED_OBJECT_NOT_ACTIVE = "referenced_object_not_active"
CAPABILITY_UNAVAILABLE = "capability_unavailable"
MODE_NOT_ENABLED = "mode_not_enabled"
TEMPLATE_MISSING_OR_DRIFTED = "template_missing_or_drifted"
MDB_ID_COLLISION = "mdb_id_collision"
MDB_BACKUP_FAILED = "mdb_backup_failed"
MDB_WRITE_FAILED = "mdb_write_failed"
POST_WRITE_VERIFICATION_FAILED = "post_write_verification_failed"
RESULT_INCOMPLETE = "result_incomplete"
ANALYSIS_FAILED = "analysis_failed"
WRITEBACK_SPOOLED = "writeback_spooled"
OPERATOR_REVIEW_REQUIRED = "operator_review_required"


class BridgeError(Exception):
    """Structured error raised by the bridge during intake/execution.

    Carries the fields defined in the automation-integrations standard so
    the caller (or eLabFTW write-back) can surface them to the operator.
    """

    def __init__(
        self,
        code: str,
        severity: Severity = Severity.ERROR,
        human_message: str = "",
        operator_hint: str = "",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.severity = severity
        self.human_message = human_message
        self.operator_hint = operator_hint
        self.retryable = retryable
        self.details = details or {}
        super().__init__(human_message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "human_message": self.human_message,
            "operator_hint": self.operator_hint,
            "retryable": self.retryable,
            "details": self.details,
        }
