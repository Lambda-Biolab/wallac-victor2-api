"""HTTP client for the Wallac Victor2 vm-agent REST API.

Wraps the vm-agent endpoints documented in ``docs/api-reference.md`` for
use by the execution orchestrator. Uses urllib (stdlib) so the bridge
core has no hard third-party dependency.

The vm-agent runs on the Windows 7 VM (libvirt NAT, port 8420) and
exposes endpoints for health, instrument, protocols, runs, jobs, and
results. This client provides typed methods for the operations the
orchestrator needs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class VmAgentError(Exception):
    """Raised when the vm-agent returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int = 0, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class VmAgentClient:
    """HTTP client for the vm-agent REST API.

    All methods raise :class:`VmAgentError` on failure.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        # URL-encode the path to handle protocol names with spaces/special chars
        import urllib.parse

        # Split path into segments and encode the last segment (e.g. protocol name)
        # while preserving leading slashes and query strings
        if "?" in path:
            base_path, query = path.split("?", 1)
        else:
            base_path, query = path, ""
        segments = base_path.split("/")
        # Encode each segment individually to preserve / separators
        encoded_segments = [urllib.parse.quote(seg, safe="") for seg in segments]
        encoded_path = "/".join(encoded_segments)
        if query:
            encoded_path += "?" + query

        url = f"{self.base}{encoded_path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content = resp.read()
                if not content:
                    return None
                return json.loads(content)
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:500]
            raise VmAgentError(
                f"vm-agent {method} {path} -> {e.code}: {detail}",
                status_code=e.code,
                detail=detail,
            ) from e
        except urllib.error.URLError as e:
            raise VmAgentError(
                f"vm-agent {method} {path} unreachable: {e}",
            ) from e

    # --- Health & instrument ---

    def get_health(self) -> dict[str, Any]:
        """GET /health — liveness + instrument connection status."""
        return self._request("GET", "/health")

    def get_status(self) -> dict[str, Any]:
        """GET /status — latest monitor snapshot (no live COM call)."""
        return self._request("GET", "/status")

    def get_instrument(self) -> dict[str, Any]:
        """GET /instrument — instrument identity and capabilities."""
        return self._request("GET", "/instrument")

    def get_protocols(self, refresh: bool = False) -> dict[str, Any]:
        """GET /protocols — list assay protocols from the instrument DB."""
        path = "/protocols"
        if refresh:
            path += "?refresh=1"
        return self._request("GET", path)

    def get_protocol(self, name_or_id: str | int) -> dict[str, Any]:
        """GET /protocols/{name|id} — resolve a single protocol."""
        return self._request("GET", f"/protocols/{name_or_id}")

    # --- Protocol PlateMap ---

    def clone_protocol(self, template_id: int, new_id: int, name: str) -> dict[str, Any]:
        """POST /mdb/protocols — clone a protocol with a new ID.

        The OEM software caches protocols in memory and doesn't re-read the
        PlateMap from the MDB. Cloning with a new ID forces a fresh read.
        """
        body = {
            "_template_id": template_id,
            "AssayProtID": new_id,
            "ProtName": name,
            "ProtNumber": new_id,
            "ProtVersion": 1,
            "FactoryPreset": False,
            "ProtGroup": 103,  # Photometry group
        }
        return self._request("POST", "/mdb/protocols", body=body)

    def delete_protocol(self, protocol_id: int) -> dict[str, Any]:
        """DELETE /mdb/protocols/{id} — remove a cloned protocol."""
        return self._request("DELETE", f"/mdb/protocols/{protocol_id}")

    def update_plate_map(self, protocol_id: int, wells: list[str]) -> dict[str, Any]:
        """PATCH /mdb/protocols/{id}/plate_map — set which wells to measure.

        Builds the 108-byte PlateMap binary (12-byte header + 96-byte 8x12
        grid) from a list of well names (e.g. ["A1", "A2", "B1", ...]).
        Must be called before start_run() to override the protocol's default
        well selection.
        """
        # 12-byte header: version=1, cols=12, rows=8 (little-endian int32)
        header = [1, 0, 0, 0, 12, 0, 0, 0, 8, 0, 0, 0]
        # 96-byte grid: row-major A1..A12, B1..B12, ... H1..H12
        grid = [0] * 96
        rows = "ABCDEFGH"
        for well in wells:
            well = well.strip().upper()
            if len(well) < 2 or well[0] not in rows:
                continue
            try:
                col = int(well[1:])
            except ValueError:
                continue
            if 1 <= col <= 12:
                row_idx = rows.index(well[0])
                grid[row_idx * 12 + (col - 1)] = 1
        plate_map = header + grid
        return self._request(
            "PATCH",
            f"/mdb/protocols/{protocol_id}/plate_map",
            body={"plate_map": plate_map},
        )

    # --- Runs ---

    def start_run(
        self,
        protocol: str | int,
        plate_id: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """POST /runs — start an assay.

        Returns:
            For dry_run: ``{"dry_run": true, "protocol_id": ...}``
            For real start: ``{"run_id": "...", "state": "running", ...}``
        """
        body: dict[str, Any] = {"protocol": protocol}
        if plate_id:
            body["plate_id"] = plate_id
        if dry_run:
            body["dry_run"] = True
        return self._request("POST", "/runs", body=body)

    def measure(
        self,
        protocol: str | int,
        wait: bool = True,
        timeout: int = 600,
        shape: str = "list",
        value: str = "od",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """POST /measure — one-shot: resolve, run, wait, return results."""
        body: dict[str, Any] = {
            "protocol": protocol,
            "wait": wait,
            "timeout": timeout,
            "shape": shape,
            "value": value,
        }
        if dry_run:
            body["dry_run"] = True
        return self._request("POST", "/measure", body=body)

    def get_run(self, run_id: str) -> dict[str, Any]:
        """GET /runs/{id} — run metadata + live block."""
        return self._request("GET", f"/runs/{run_id}")

    def get_run_results(
        self,
        run_id: str,
        shape: str = "list",
        value: str = "od",
        dedup: bool = True,
    ) -> dict[str, Any]:
        """GET /runs/{id}/results — per-well results for a run."""
        params = f"shape={shape}&value={value}&dedup={'1' if dedup else '0'}"
        return self._request("GET", f"/runs/{run_id}/results?{params}")

    def abort_run(self, run_id: str) -> dict[str, Any]:
        """POST /runs/{id}/abort — cancel a run."""
        return self._request("POST", f"/runs/{run_id}/abort")

    def delete_run(self, run_id: str, force: bool = False) -> dict[str, Any]:
        """DELETE /runs/{id} — forget a finished/failed run."""
        path = f"/runs/{run_id}"
        if force:
            path += "?force=1"
        return self._request("DELETE", path)

    # --- Jobs (completed assays) ---

    def get_jobs(self) -> dict[str, Any]:
        """GET /jobs — list completed assay jobs."""
        return self._request("GET", "/jobs")

    def get_job_results(
        self,
        job_id: int,
        shape: str = "list",
        value: str = "od",
        dedup: bool = True,
    ) -> dict[str, Any]:
        """GET /jobs/{id}/results — per-well results for a completed job."""
        params = f"shape={shape}&value={value}&dedup={'1' if dedup else '0'}"
        return self._request("GET", f"/jobs/{job_id}/results?{params}")

    def export_job_results(
        self,
        job_id: int,
        format: str = "long",
        value: str = "raw",
    ) -> str:
        """GET /jobs/{id}/export — CSV export of job results."""
        params = f"format={format}&value={value}"
        url = f"{self.base}/jobs/{job_id}/export?{params}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode()

    # --- Admin ---

    def reconnect(self) -> dict[str, Any]:
        """POST /admin/reconnect — drop and recreate COM connection."""
        return self._request("POST", "/admin/reconnect")
