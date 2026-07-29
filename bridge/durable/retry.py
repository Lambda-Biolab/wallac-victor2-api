"""Retry-policy helpers for the durable writeback worker.

The retry policy distinguishes transient failures (retry with bounded
exponential backoff + jitter) from permanent failures (pause for
operator action; never retry silently).

Permanent-failure classes (issue #44 §"Retry policy"):

    * TLS chain or hostname failure
    * invalid/unreadable CA bundle
    * HTTP 401/403
    * schema or payload errors
    * conflicting remote state

TLS verification must never be disabled by retry logic; this helper
takes a permanent-failure tag explicitly so the call site can flag
auth/TLS / CA / schema errors as permanent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Transient HTTP statuses (issue #44 §"Retry policy").
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Backoff:
    """Bounded exponential backoff schedule with full jitter.

    AWS-style: the wait is a uniform random sample in ``[0, base * 2^attempt]``,
    capped at ``cap``. We cap attempts at ``max_attempts`` so a long-running
    job cannot sit paused forever.
    """

    base_seconds: float = 30.0
    cap_seconds: float = 60.0 * 60.0  # 1 hour
    max_attempts: int = 8

    def wait_seconds(self, attempt: int) -> float:
        if attempt < 0:
            return 0.0
        if attempt >= self.max_attempts:
            return float(self.cap_seconds)
        upper = min(self.base_seconds * (2**attempt), self.cap_seconds)
        # ``random.uniform`` is fine for full-jitter backoff timing: the
        # value is not security-sensitive, just a sleep delay. See S311.
        return random.uniform(0.0, upper)  # noqa: S311


# Error kinds the call site can pass to mark a step permanent
# without having to map every failure mode to an HTTP status. See
# ``classify_status`` / issue #44.
PERMANENT_ERROR_KINDS = frozenset({"tls", "ca_bundle", "auth", "schema", "payload"})


def classify_status(
    http_status: int | None,
    *,
    tls_error: bool = False,
    error_kind: str | None = None,
) -> str:
    """Return ``transient``, ``permanent``, or ``success`` for a writeback step.

    ``tls_error`` overrides the status check so callers that catch a
    TLS error before they ever get an HTTP response still mark the
    step permanent (per issue #44). ``error_kind`` is the structured
    classification: ``"tls"``, ``"ca_bundle"``, ``"auth"``,
    ``"schema"``, ``"payload"`` always mean permanent regardless of
    HTTP status (the eLabFTW server may return a generic 4xx for any
    of them).
    """
    if tls_error or (error_kind and error_kind in PERMANENT_ERROR_KINDS):
        return "permanent"
    if http_status is None:
        return "transient"
    if 200 <= http_status < 300:
        return "success"
    if http_status in TRANSIENT_HTTP_STATUSES:
        return "transient"
    if http_status in (401, 403, 410, 422):
        return "permanent"
    # Anything else (404, 409, 5xx outside the transient set) — treat
    # as transient for writeback. Operators can override per-step.
    return "transient"


def is_permanent(outcome: str) -> bool:
    return outcome == "permanent"
