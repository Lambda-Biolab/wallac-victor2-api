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
