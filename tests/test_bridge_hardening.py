"""Acceptance criteria tests for issue #6: auth/network hardening.

AC: "Auth/secrets policy documented in the Wallac repo."
AC: "Dashboard endpoints reject unauthorized public access."
AC: "Tests or smoke checks verify no service secrets appear in rendered
dashboard HTML/JS payloads."
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from bridge.config import (
    DEFAULT_DASHBOARD_PORT,
    ENV_ELABFTW_API_KEY,
    BridgeConfig,
    ConfigError,
)
from bridge.dashboard import (
    DashboardJobState,
    DashboardServer,
    DashboardStateStore,
)
from bridge.secrets_check import scan_for_secrets

# --- AC 1: Config validates env-only secrets --------------------------------


def test_config_requires_api_key():
    """ConfigError is raised when ELABFTW_API_KEY is missing."""
    with pytest.raises(ConfigError) as exc_info:
        BridgeConfig.from_env(env={})
    assert ENV_ELABFTW_API_KEY in str(exc_info.value)
    assert "do NOT use a human admin key" in str(exc_info.value)


def test_config_requires_nonempty_api_key():
    """Empty API key is rejected."""
    with pytest.raises(ConfigError):
        BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "   "})


def test_config_from_env_with_secrets():
    """Config loads secrets from env vars."""
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-testkey123",
            "WALLAC_ELABFTW_URL": "https://elab.local:3148",
            "WALLAC_DASHBOARD_TOKEN": "session-secret",
            "WALLAC_DASHBOARD_PORT": "9999",
        }
    )
    assert config.elabftw_api_key == "5-testkey123"
    assert config.elabftw_url == "https://elab.local:3148"
    assert config.dashboard_token == "session-secret"
    assert config.dashboard_port == 9999
    assert config.dashboard_requires_auth is True


def test_config_redacted_hides_secrets():
    """redacted() masks all secret values."""
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-secretkey123",
            "WALLAC_VM_AGENT_TOKEN": "vm-secret",
            "WALLAC_DASHBOARD_TOKEN": "dash-secret",
        }
    )
    redacted = config.redacted()
    assert redacted["elabftw_api_key"] == "***REDACTED***"
    assert redacted["vm_agent_token"] == "***REDACTED***"
    assert redacted["dashboard_token"] == "***REDACTED***"
    # Non-secret values are visible
    assert redacted["elabftw_url"] == config.elabftw_url
    assert redacted["dashboard_port"] == str(config.dashboard_port)


def test_config_redacted_shows_unset_as_unset():
    """Unset optional secrets show as (unset), not REDACTED."""
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    redacted = config.redacted()
    assert redacted["vm_agent_token"] == "(unset)"
    assert redacted["dashboard_token"] == "(unset)"


def test_config_no_auth_without_dashboard_token():
    """Without a dashboard token, auth is not enforced."""
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.dashboard_requires_auth is False


# --- AC 2: Dashboard rejects unauthorized access ---------------------------


@pytest.fixture
def secured_server():
    """Dashboard server with a session token (auth enforced)."""
    store = DashboardStateStore()
    srv = DashboardServer(
        state_store=store,
        abort_handler=None,
        host="127.0.0.1",
        port=0,
        session_token="test-session-token",
    )
    srv.start()
    time.sleep(0.1)
    yield srv, store
    srv.stop()


def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_dashboard_rejects_no_token(secured_server):
    """Without a token, all endpoints return 401."""
    srv, store = secured_server
    base = f"http://{srv.address[0]}:{srv.address[1]}"

    code, _ = _get(f"{base}/")
    assert code == 401

    code, _ = _get(f"{base}/api/jobs/1")
    assert code == 401


def test_dashboard_rejects_wrong_token(secured_server):
    """With a wrong token, endpoints return 401."""
    srv, store = secured_server
    base = f"http://{srv.address[0]}:{srv.address[1]}"

    code, _ = _get(f"{base}/", token="wrong-token")
    assert code == 401


def test_dashboard_accepts_correct_token(secured_server):
    """With the correct token, endpoints return 200."""
    srv, store = secured_server
    base = f"http://{srv.address[0]}:{srv.address[1]}"

    code, body = _get(f"{base}/", token="test-session-token")
    assert code == 200
    assert b"Wallac Victor2" in body


def test_dashboard_api_requires_token(secured_server):
    """The JSON API also requires the session token."""
    srv, store = secured_server
    base = f"http://{srv.address[0]}:{srv.address[1]}"

    # Without token → 401
    code, _ = _get(f"{base}/api/jobs/1")
    assert code == 401

    # With token → 200 (or 404 if job doesn't exist, but not 401)
    code, _ = _get(f"{base}/api/jobs/1", token="test-session-token")
    assert code == 404  # job not found, but auth passed


# --- AC 3: No secrets in rendered dashboard HTML/JS ------------------------


def test_dashboard_html_has_no_secrets():
    """The dashboard HTML file contains no secret values or patterns."""
    from pathlib import Path

    html_path = Path(__file__).resolve().parent.parent / "bridge" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    result = scan_for_secrets(
        html,
        known_secrets=(
            "5-testkey123",  # sample API key
            "test-session-token",  # sample session token
        ),
    )
    assert result.clean, f"Secret leakage found: {result.findings}"


def test_dashboard_html_no_secret_keywords():
    """The dashboard HTML has no secret keywords in value-assignment context."""
    from pathlib import Path

    html_path = Path(__file__).resolve().parent.parent / "bridge" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    result = scan_for_secrets(html)
    # The HTML should be clean of secret patterns
    # (keyword checks only flag keyword + value assignment, not CSS class names)
    assert result.clean, f"Secret patterns found: {result.findings}"


def test_job_state_json_has_no_secrets():
    """DashboardJobState.to_dict() contains no secret fields."""
    state = DashboardJobState(
        item_id=1,
        title="Test",
        device_identity="victor2-123",
    )
    d = state.to_dict()
    json_str = json.dumps(d)

    result = scan_for_secrets(
        json_str,
        known_secrets=(
            "5-testkey123",
            "vm-secret",
            "session-token",
        ),
    )
    assert result.clean, f"Secret leakage in job state JSON: {result.findings}"


# --- Bonus: secrets_check catches real leaks --------------------------------


def test_secrets_check_detects_api_key():
    """scan_for_secrets detects an API key in content."""
    html = '<script>const apiKey = "5-secretkey123";</script>'
    result = scan_for_secrets(html)
    assert not result.clean
    assert any(f["type"] == "secret_keyword" for f in result.findings)


def test_secrets_check_detects_bearer_token():
    """scan_for_secrets detects a Bearer token in content."""
    html = "Authorization: Bearer abc123def456"
    result = scan_for_secrets(html)
    assert not result.clean


def test_secrets_check_detects_known_secret():
    """scan_for_secrets detects a known secret value."""
    html = "<p>The key is 5-mysecretkey456</p>"
    result = scan_for_secrets(html, known_secrets=("5-mysecretkey456",))
    assert not result.clean
    assert any(f["type"] == "known_secret" for f in result.findings)


def test_secrets_check_clean_content():
    """scan_for_secrets returns clean for safe content."""
    html = "<div>Wallac Victor2 Live Monitor</div><p>State: running</p>"
    result = scan_for_secrets(html)
    assert result.clean


def test_secrets_check_ignores_short_known_secrets():
    """Known secrets shorter than 4 chars are not checked (false positives)."""
    html = "<p>ab</p>"
    result = scan_for_secrets(html, known_secrets=("ab",))
    assert result.clean


# --- Bonus: config defaults -------------------------------------------------


def test_config_defaults():
    """Config uses sensible defaults when optional vars are unset."""
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.dashboard_host == "0.0.0.0"
    assert config.dashboard_port == DEFAULT_DASHBOARD_PORT
    assert config.bridge_identity == "wallac-bridge"
    assert config.elabftw_category == 9


def test_live_monitor_url_base():
    """live_monitor_url_base produces the correct URL."""
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-key",
            "WALLAC_DASHBOARD_HOST": "wallac.local",
            "WALLAC_DASHBOARD_PORT": "8421",
        }
    )
    assert config.live_monitor_url_base == "http://wallac.local:8421"
