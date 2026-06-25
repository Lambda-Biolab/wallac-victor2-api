"""Acceptance criteria tests for issue #3: graphical live dashboard.

AC: "Dashboard consumes the bridge live progress stream for sub-second
updates."

AC: "Browser never receives eLabFTW API keys or other service secrets."

AC: "Dashboard gracefully reconnects after network interruption."

These tests exercise the dashboard HTTP server with real HTTP requests
(via urllib) against a running server on a random port.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest
from bridge_fixtures import make_extra_fields

from bridge.abort import DashboardAbortHandler
from bridge.dashboard import (
    DashboardJobState,
    DashboardServer,
    DashboardStateStore,
    PreflightCheck,
)
from bridge.lifecycle import LifecycleManager
from bridge.models import AutomationJob, RequestFields

# --- Helpers ----------------------------------------------------------------


@pytest.fixture
def server():
    """Start a dashboard server on a random port, return (server, store, abort_handler)."""
    store = DashboardStateStore()
    abort_handler = DashboardAbortHandler()
    srv = DashboardServer(
        state_store=store,
        abort_handler=abort_handler,
        host="127.0.0.1",
        port=0,  # random port
    )
    srv.start()
    time.sleep(0.1)  # let the server bind
    yield srv, store, abort_handler
    srv.stop()


def _base_url(server: DashboardServer) -> str:
    host, port = server.address
    return f"http://{host}:{port}"


def _get(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def _post(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def make_state(item_id: int = 1, **kwargs) -> DashboardJobState:
    defaults = dict(
        item_id=item_id,
        title="Test Automation Job",
        experiment_id="42",
        wallac_run_id="r-ab12cd34",
        requester="test@example.org",
        operator="Test Operator",
        device_identity="victor2-serial-4200123",
        service_version="0.1.0",
        state="running",
        progress_percent=42.0,
        current_step="Measuring plate",
        started_at="2026-06-25T12:00:00+00:00",
        last_heartbeat="2026-06-25T12:01:00+00:00",
        elapsed_seconds=60.0,
        protocol_name="Absorbance @ 405",
        plate_layout_reference="https://elab.local/experiments/42",
        expected_outputs="8 wells, OD 0.0-2.0",
        request_checksum="abc123def456",
        preflight=[
            PreflightCheck(name="Protocol found", passed=True),
            PreflightCheck(name="Instrument ready", passed=True),
            PreflightCheck(name="Plate loaded", passed=False, detail="No plate detected"),
        ],
        plate_wells={"A01": 0.071, "A02": 0.123, "H12": 0.987},
        event_log=["[12:00] Run started", "[12:01] Measuring plate"],
        artifacts=[
            {"filename": "results.csv", "checksum": "abc123", "size": 256},
        ],
        result_summary="8 wells measured",
        writeback_status="pending",
        writeback_last_retry="",
        writeback_operator_hint="",
    )
    defaults.update(kwargs)
    return DashboardJobState(**defaults)


# --- AC 1: Dashboard HTML is served -----------------------------------------


def test_dashboard_html_served(server):
    """GET / returns the dashboard HTML page."""
    srv, store, _ = server
    code, body, content_type = _get(f"{_base_url(srv)}/")
    assert code == 200
    assert "text/html" in content_type
    html = body.decode("utf-8")
    assert "Wallac Victor2" in html
    assert "Live Monitor" in html
    # All 9 panels should be present
    assert "Job Header" in html
    assert "State banner" in html or "state-banner" in html
    assert "Preflight" in html
    assert "Request Snapshot" in html
    assert "Plate View" in html
    assert "Event Log" in html or "event-log" in html
    assert "Controls" in html
    assert "Results" in html and "Artifacts" in html
    assert "Write-back" in html


# --- AC 2: Browser never receives secrets -----------------------------------


def test_no_secrets_in_html(server):
    """The dashboard HTML must not contain any API keys or secrets."""
    srv, store, _ = server
    code, body, _ = _get(f"{_base_url(srv)}/")
    html = body.decode("utf-8")
    # No API key patterns
    assert "api_key" not in html.lower()
    assert "apikey" not in html.lower()
    assert "authorization" not in html.lower()
    assert "bearer" not in html.lower()
    assert "secret" not in html.lower()
    assert "password" not in html.lower()
    assert "token" not in html.lower()


def test_no_secrets_in_job_state_json(server):
    """The job state JSON must not contain any API keys or secrets."""
    srv, store, _ = server
    store.update(1, make_state(item_id=1))
    code, body, _ = _get(f"{_base_url(srv)}/api/jobs/1")
    assert code == 200
    data = json.loads(body)
    # Check no secret fields
    json_str = json.dumps(data)
    assert "api_key" not in json_str.lower()
    assert "apikey" not in json_str.lower()
    assert "secret" not in json_str.lower()
    assert "password" not in json_str.lower()
    assert "token" not in json_str.lower()


# --- AC 3: Job state JSON API -----------------------------------------------


def test_job_state_json(server):
    """GET /api/jobs/{id} returns the full job state as JSON."""
    srv, store, _ = server
    store.update(1, make_state(item_id=1))
    code, body, content_type = _get(f"{_base_url(srv)}/api/jobs/1")
    assert code == 200
    assert "application/json" in content_type
    data = json.loads(body)
    assert data["item_id"] == 1
    assert data["state"] == "running"
    assert data["progress_percent"] == 42.0
    assert data["protocol_name"] == "Absorbance @ 405"
    assert data["wallac_run_id"] == "r-ab12cd34"
    assert len(data["preflight"]) == 3
    assert data["preflight"][0]["passed"] is True
    assert data["plate_wells"]["A01"] == 0.071
    assert len(data["event_log"]) == 2
    assert len(data["artifacts"]) == 1


def test_job_not_found(server):
    """GET /api/jobs/{id} for a non-existent job returns 404."""
    srv, store, _ = server
    code, body, _ = _get(f"{_base_url(srv)}/api/jobs/999")
    assert code == 404
    data = json.loads(body)
    assert data["error"] == "job_not_found"


# --- AC 4: SSE stream --------------------------------------------------------


def test_sse_stream_produces_events(server):
    """GET /api/jobs/{id}/stream produces SSE events with job state."""
    srv, store, _ = server
    store.update(1, make_state(item_id=1, state="running"))

    # Read the first few bytes of the SSE stream
    url = f"{_base_url(srv)}/api/jobs/1/stream"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")
        # Read the retry hint + first data event
        data = resp.read(4096).decode("utf-8")
        assert "retry:" in data  # reconnection hint
        assert "data:" in data  # at least one event
        # The data should be valid JSON
        for line in data.split("\n"):
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                assert payload["item_id"] == 1
                assert payload["state"] == "running"
                break


def test_sse_stream_has_retry_hint(server):
    """The SSE stream includes a retry hint for reconnection."""
    srv, store, _ = server
    store.update(1, make_state(item_id=1))
    url = f"{_base_url(srv)}/api/jobs/1/stream"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = resp.read(1024).decode("utf-8")
        # retry: hint tells the browser to reconnect after N ms
        assert "retry:" in data


# --- AC 5: Abort endpoint ---------------------------------------------------


def test_abort_endpoint(server):
    """POST /api/jobs/{id}/abort requests a controlled abort."""
    srv, store, abort_handler = server

    # Register a job with the abort handler (start from accepted, progress to running)
    job = AutomationJob(
        item_id=1,
        title="Test",
        state="accepted",
        request_fields=RequestFields(),
        extra_fields=make_extra_fields(),
    )
    lifecycle = LifecycleManager(job)
    lifecycle.start_validation()
    lifecycle.validation_passed()
    lifecycle.start_run()
    abort_handler.register(1, lifecycle)

    code, body = _post(f"{_base_url(srv)}/api/jobs/1/abort")
    assert code == 200
    data = json.loads(body)
    assert data["abort_source"] == "dashboard"
    assert data["new_state"] == "abort_requested"
    assert lifecycle.state.value == "abort_requested"


def test_abort_not_active(server):
    """POST /api/jobs/{id}/abort for a non-active job returns 404."""
    srv, store, _ = server
    code, body = _post(f"{_base_url(srv)}/api/jobs/999/abort")
    assert code == 404


# --- AC 6: Auth (session token) --------------------------------------------


def test_unauthorized_without_token():
    """Without a session token, requests are rejected."""
    store = DashboardStateStore()
    srv = DashboardServer(
        state_store=store,
        abort_handler=None,
        host="127.0.0.1",
        port=0,
        session_token="secret-token",
    )
    srv.start()
    time.sleep(0.1)
    try:
        code, body, _ = _get(f"http://{srv.address[0]}:{srv.address[1]}/api/jobs/1")
        assert code == 401
    finally:
        srv.stop()


def test_authorized_with_token():
    """With the correct session token, requests succeed."""
    store = DashboardStateStore()
    store.update(1, make_state(item_id=1))
    srv = DashboardServer(
        state_store=store,
        abort_handler=None,
        host="127.0.0.1",
        port=0,
        session_token="secret-token",
    )
    srv.start()
    time.sleep(0.1)
    try:
        url = f"http://{srv.address[0]}:{srv.address[1]}/api/jobs/1"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer secret-token")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["item_id"] == 1
    finally:
        srv.stop()


# --- AC 7: Artifact download ------------------------------------------------


def test_artifact_download(server):
    """GET /api/jobs/{id}/artifacts/{filename} downloads an artifact."""
    srv, store, _ = server
    # The artifact store is on the server
    csv_content = b"well,od\nA01,0.071\n"
    srv._httpd.artifact_store[(1, "results.csv")] = (csv_content, "text/csv")  # type: ignore[attr-defined]

    code, body, content_type = _get(f"{_base_url(srv)}/api/jobs/1/artifacts/results.csv")
    assert code == 200
    assert "text/csv" in content_type
    assert body == csv_content


def test_artifact_not_found(server):
    """GET /api/jobs/{id}/artifacts/{filename} for missing file returns 404."""
    srv, store, _ = server
    code, _, _ = _get(f"{_base_url(srv)}/api/jobs/1/artifacts/nonexistent.csv")
    assert code == 404


# --- Bonus: state store updates ---------------------------------------------


def test_state_store_update_and_get():
    """DashboardStateStore stores and retrieves job states."""
    store = DashboardStateStore()
    state = make_state(item_id=1)
    store.update(1, state)
    assert store.get(1) is state
    assert store.get(999) is None
    store.remove(1)
    assert store.get(1) is None


def test_state_store_to_dict_no_secrets():
    """DashboardJobState.to_dict() contains no secret fields."""
    state = make_state(item_id=1)
    d = state.to_dict()
    json_str = json.dumps(d)
    assert "api_key" not in json_str.lower()
    assert "secret" not in json_str.lower()
    assert "password" not in json_str.lower()
    assert "token" not in json_str.lower()
