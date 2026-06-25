"""Graphical live dashboard server for the Wallac bridge.

Implements issue #3: a browser dashboard served by the bridge, linked from
eLabFTW through the Automation Job ``Live Monitor`` URL field.

The dashboard is a single-page HTML/CSS/JS application that consumes:
  - ``GET /api/jobs/{id}`` — JSON job state (all 9 panels' data)
  - ``GET /api/jobs/{id}/stream`` — SSE live progress stream (sub-second)
  - ``POST /api/jobs/{id}/abort`` — request controlled abort
  - ``GET /api/jobs/{id}/artifacts/{filename}`` — download result artifacts

The browser never receives eLabFTW API keys or service secrets — the bridge
backend holds all credentials and proxies data to the frontend.

SSE uses ``retry:`` hints so the browser's ``EventSource`` auto-reconnects
after network interruption.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"

# SSE retry hint (ms) — tells the browser to reconnect after this delay
SSE_RETRY_MS = 3000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Dashboard state --------------------------------------------------------


@dataclass
class PreflightCheck:
    """A single preflight checklist item."""

    name: str
    passed: bool = False
    detail: str = ""


@dataclass
class DashboardJobState:
    """Live state for a single job, served to the dashboard frontend.

    This is the bridge-side state that the dashboard reads via the JSON
    API and SSE stream.  It contains no secrets — only operator-visible
    information.
    """

    item_id: int
    title: str = ""
    experiment_id: str = ""
    wallac_run_id: str = ""
    requester: str = ""
    operator: str = ""
    device_identity: str = ""
    service_version: str = ""
    state: str = "accepted"
    progress_percent: float = 0.0
    current_step: str = ""
    started_at: str = ""
    last_heartbeat: str = ""
    elapsed_seconds: float = 0.0

    # Request snapshot (signed, immutable)
    protocol_name: str = ""
    plate_layout_reference: str = ""
    expected_outputs: str = ""
    request_checksum: str = ""

    # Preflight
    preflight: list[PreflightCheck] = field(default_factory=list)

    # Plate view (96-well)
    plate_wells: dict[str, Any] = field(default_factory=dict)

    # Event log
    event_log: list[str] = field(default_factory=list)

    # Results/artifacts
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    result_summary: str = ""

    # Write-back status
    writeback_status: str = "pending"  # pending | succeeded | failed
    writeback_last_retry: str = ""
    writeback_operator_hint: str = ""

    # Errors
    last_error_code: str = ""
    operator_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for JSON API responses.

        This dict is sent to the browser — it must never contain secrets.
        """
        return {
            "item_id": self.item_id,
            "title": self.title,
            "experiment_id": self.experiment_id,
            "wallac_run_id": self.wallac_run_id,
            "requester": self.requester,
            "operator": self.operator,
            "device_identity": self.device_identity,
            "service_version": self.service_version,
            "state": self.state,
            "progress_percent": self.progress_percent,
            "current_step": self.current_step,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
            "elapsed_seconds": self.elapsed_seconds,
            "protocol_name": self.protocol_name,
            "plate_layout_reference": self.plate_layout_reference,
            "expected_outputs": self.expected_outputs,
            "request_checksum": self.request_checksum,
            "preflight": [
                {"name": p.name, "passed": p.passed, "detail": p.detail} for p in self.preflight
            ],
            "plate_wells": self.plate_wells,
            "event_log": self.event_log,
            "artifacts": self.artifacts,
            "result_summary": self.result_summary,
            "writeback_status": self.writeback_status,
            "writeback_last_retry": self.writeback_last_retry,
            "writeback_operator_hint": self.writeback_operator_hint,
            "last_error_code": self.last_error_code,
            "operator_hint": self.operator_hint,
        }


class DashboardStateStore:
    """Thread-safe store of dashboard states for active jobs.

    The execution loop updates states via :meth:`update`; the dashboard
    server reads them via :meth:`get`.  An SSE condition variable
    notifies streaming clients of updates.
    """

    def __init__(self) -> None:
        self._states: dict[int, DashboardJobState] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def get(self, item_id: int) -> DashboardJobState | None:
        with self._lock:
            return self._states.get(item_id)

    def update(self, item_id: int, state: DashboardJobState) -> None:
        with self._cond:
            self._states[item_id] = state
            self._cond.notify_all()

    def remove(self, item_id: int) -> None:
        with self._cond:
            self._states.pop(item_id, None)
            self._cond.notify_all()

    def wait_for_update(self, item_id: int, timeout: float = 1.0) -> bool:
        """Block until the job state changes or timeout.

        Returns True if notified (state may have changed), False on timeout.
        """
        with self._cond:
            return self._cond.wait_for(timeout)


# --- HTTP server ------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard server."""

    server_version = "WallacBridgeDashboard/0.1"

    # Suppress default logging (the bridge logs its own messages)
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.server.session_token  # type: ignore[attr-defined]
        if token is None:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + token

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # --- Routing ---

    # Terminal states that end the SSE stream
    _TERMINAL_STATES = frozenset(
        {
            "completed",
            "failed",
            "aborted",
            "unknown_requires_operator_review",
        }
    )

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        path = unquote(self.path)
        if self._route_get(path):
            return
        self._send_json(404, {"error": "not_found", "path": path})

    def _route_get(self, path: str) -> bool:
        """Route a GET path. Returns True if handled."""
        if path in ("/", "/dashboard"):
            self._serve_dashboard_html()
            return True
        if not path.startswith("/api/jobs/"):
            return False

        parts = path.strip("/").split("/")
        # SSE stream: /api/jobs/{id}/stream
        if path.endswith("/stream"):
            item_id = self._extract_job_id(path, "/stream")
            if item_id is not None:
                self._serve_sse_stream(item_id)
                return True
            return False
        # Job state: /api/jobs/{id}
        if len(parts) == 3 and parts[1] == "jobs":
            item_id = self._extract_job_id(path)
            if item_id is not None:
                self._serve_job_state(item_id)
                return True
            return False
        # Artifact: /api/jobs/{id}/artifacts/{filename}
        if len(parts) >= 5 and parts[3] == "artifacts":
            self._serve_artifact(int(parts[2]), "/".join(parts[4:]))
            return True
        return False

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        path = unquote(self.path)

        # Abort: POST /api/jobs/{id}/abort
        if path.startswith("/api/jobs/") and path.endswith("/abort"):
            item_id = self._extract_job_id(path, "/abort")
            if item_id is not None:
                self._handle_abort(item_id)
                return

        self._send_json(404, {"error": "not_found", "path": path})

    def _extract_job_id(self, path: str, suffix: str = "") -> int | None:
        try:
            stripped = path.rstrip("/")
            if suffix:
                stripped = stripped[: -len(suffix)] if stripped.endswith(suffix) else stripped
            return int(stripped.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return None

    # --- Route handlers ---

    def _serve_dashboard_html(self) -> None:
        try:
            html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
            self._send_html(200, html)
        except FileNotFoundError:
            self._send_json(500, {"error": "dashboard_html_not_found"})

    def _serve_job_state(self, item_id: int) -> None:
        store: DashboardStateStore = self.server.state_store  # type: ignore[attr-defined]
        state = store.get(item_id)
        if state is None:
            self._send_json(404, {"error": "job_not_found", "item_id": item_id})
            return
        self._send_json(200, state.to_dict())

    def _serve_sse_stream(self, item_id: int) -> None:
        store: DashboardStateStore = self.server.state_store  # type: ignore[attr-defined]
        state = store.get(item_id)
        if state is None:
            self._send_json(404, {"error": "job_not_found", "item_id": item_id})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self.wfile.write(f"retry: {SSE_RETRY_MS}\n\n".encode())
            self.wfile.flush()
            self._send_sse_event(state.to_dict())
            self._stream_updates(store, item_id)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_updates(self, store: DashboardStateStore, item_id: int) -> None:
        """Stream SSE updates until terminal state or disconnect."""
        while True:
            if store.wait_for_update(item_id, timeout=1.0):
                new_state = store.get(item_id)
                if new_state is None:
                    break
                self._send_sse_event(new_state.to_dict())
                if new_state.state in self._TERMINAL_STATES:
                    break
            else:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()

    def _send_sse_event(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.wfile.write(b"data: " + payload + b"\n\n")
        self.wfile.flush()

    def _handle_abort(self, item_id: int) -> None:
        handler = self.server.abort_handler  # type: ignore[attr-defined]
        if handler is None or not handler.is_registered(item_id):
            self._send_json(404, {"error": "job_not_active", "item_id": item_id})
            return

        result = handler.request_abort(item_id)
        self._send_json(200, result)

    def _serve_artifact(self, item_id: int, filename: str) -> None:
        artifacts: dict = getattr(self.server, "artifact_store", {})  # type: ignore[attr-defined]
        key = (item_id, filename)
        if key not in artifacts:
            self._send_json(404, {"error": "artifact_not_found"})
            return

        content, content_type = artifacts[key]
        body = content if isinstance(content, bytes) else content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashboardServer:
    """Serves the graphical dashboard and JSON API.

    Usage::

        server = DashboardServer(port=8421, state_store=store,
                                  abort_handler=abort_handler)
        server.start()
        # ... serve until done ...
        server.stop()
    """

    def __init__(
        self,
        *,
        state_store: DashboardStateStore,
        abort_handler: Any | None = None,
        artifact_store: dict[tuple[int, str], tuple[bytes, str]] | None = None,
        host: str = "0.0.0.0",
        port: int = 8421,
        session_token: str | None = None,
    ) -> None:
        self._httpd = ThreadingHTTPServer((host, port), DashboardHandler)
        self._httpd.state_store = state_store  # type: ignore[attr-defined]
        self._httpd.abort_handler = abort_handler  # type: ignore[attr-defined]
        self._httpd.artifact_store = artifact_store or {}  # type: ignore[attr-defined]
        self._httpd.session_token = session_token  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Dashboard server started on %s", self._httpd.server_address)

    def stop(self) -> None:
        self._httpd.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        self._httpd.server_close()
        logger.info("Dashboard server stopped")

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address  # type: ignore[return-value]
