"""Secret leakage smoke checks for the Wallac bridge dashboard.

Implements issue #6 AC: "Tests or smoke checks verify no service secrets
appear in rendered dashboard HTML/JS payloads."

The bridge serves a dashboard to the operator's browser.  The browser must
never receive the eLabFTW service API key, the vm-agent token, or any other
service secret.  This module scans rendered HTML/JS output for known secret
values and common secret patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Patterns that indicate a secret in HTML/JS ----------------------------

# Literal substrings that should never appear in browser-facing output
SECRET_KEYWORDS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "bearer",
    "secret",
    "password",
    "passwd",
    "token",
    "private_key",
    "privkey",
)

# Regex patterns for common secret formats
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"\b\d+-[A-Za-z0-9]{20,}\b"),  # eLabFTW API key format: N-xxxxx
    re.compile(r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"),
)


@dataclass
class SecretCheckResult:
    """Result of a secret leakage scan."""

    clean: bool
    findings: list[dict[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.clean


def scan_for_secrets(
    content: str,
    *,
    known_secrets: tuple[str, ...] = (),
) -> SecretCheckResult:
    """Scan rendered HTML/JS content for secret leakage.

    Args:
        content: The HTML/JS text to scan.
        known_secrets: Specific secret values to check for (e.g., the actual
                       API key, session token).  These are checked as literal
                       substrings.

    Returns:
        :class:`SecretCheckResult` with ``clean=True`` if no secrets found,
        or a list of findings.
    """
    findings: list[dict[str, str]] = []

    # 1. Check for known secret values (literal substring match)
    for secret in known_secrets:
        if secret and len(secret) >= 4 and secret in content:
            findings.append(
                {
                    "type": "known_secret",
                    "detail": f"Literal secret value found in output (first 8 chars: {secret[:8]}...)",
                }
            )

    # 2. Check for secret keywords in suspicious contexts
    # (only flag if the keyword appears near a value assignment, not in
    # a CSS class name or display text)
    for keyword in SECRET_KEYWORDS:
        # Look for keyword followed by a value (e.g., "token: 'xxx'" or "api_key=xxx")
        pattern = re.compile(
            rf"\b{re.escape(keyword)}\b\s*[:=]\s*['\"][^'\"]{{4,}}",
            re.IGNORECASE,
        )
        if pattern.search(content):
            findings.append(
                {
                    "type": "secret_keyword",
                    "detail": f"Keyword '{keyword}' found with an assigned value",
                }
            )

    # 3. Check for secret format patterns
    for pattern in SECRET_PATTERNS:
        match = pattern.search(content)
        if match:
            findings.append(
                {
                    "type": "secret_pattern",
                    "detail": f"Secret pattern matched: {match.group()[:40]}",
                }
            )

    # 4. Check for the word "token" in JavaScript variable assignments
    # (the dashboard JS should not reference any token variable)
    token_var = re.compile(r"\bvar\s+\w*token\w*\s*=", re.IGNORECASE)
    if token_var.search(content):
        findings.append(
            {
                "type": "token_variable",
                "detail": "JavaScript variable with 'token' in the name found",
            }
        )

    return SecretCheckResult(
        clean=len(findings) == 0,
        findings=findings,
    )


def scan_dashboard_html(html: str, *, known_secrets: tuple[str, ...] = ()) -> SecretCheckResult:
    """Scan the dashboard HTML page for secret leakage.

    Convenience wrapper around :func:`scan_for_secrets` for the dashboard.
    """
    return scan_for_secrets(html, known_secrets=known_secrets)
