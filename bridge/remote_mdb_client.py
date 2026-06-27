"""Remote MDB client: implements MdbClient Protocol via HTTP to vm-agent.

Stage 5 of docs/plans/wallac-protocol-authoring.md.

The bridge runs on Linux and cannot access the Wallac MDB (Jet database)
directly — pyodbc/DAO are Windows-only. This module provides a
:class:`RemoteMdbClient` that implements the :class:`~bridge.generated_protocols.MdbClient`
Protocol by calling the vm-agent's ``/mdb/*`` HTTP endpoints.

The vm-agent (running on the Windows 7 VM) handles the actual DAO/COM
operations. The bridge's :class:`~bridge.generated_protocols.GeneratedProtocolManager`
uses this client as its ``mdb_client`` dependency in production.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from .generated_protocols import MdbClient
from .vm_agent_client import VmAgentError

logger = logging.getLogger(__name__)


class RemoteMdbClient(MdbClient):
    """Implements :class:`MdbClient` via HTTP calls to the vm-agent.

    All methods raise :class:`VmAgentError` on failure.
    404 responses are translated to ``None`` / ``False`` / ``0`` as
    appropriate for each method's contract.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base}{path}"
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
            if e.code == 404:
                return None  # caller translates None to the right return value
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:500]
            raise VmAgentError(
                f"vm-agent MDB {method} {path} -> {e.code}: {detail}",
                status_code=e.code,
                detail=detail,
            ) from e
        except urllib.error.URLError as e:
            raise VmAgentError(
                f"vm-agent MDB {method} {path} unreachable: {e}",
            ) from e

    # --- MdbClient implementation ---

    def get_protocol_group_id(self, group_name: str) -> int | None:
        """GET /mdb/groups?name=<name> — ProtocolGroup ID lookup."""
        result = self._request("GET", f"/mdb/groups?name={quote(group_name)}")
        if result is None:
            return None
        return result.get("group_id")

    def get_protocol(self, assay_prot_id: int) -> dict[str, Any] | None:
        """GET /mdb/protocols/<id> — full AssayProtocol row."""
        return self._request("GET", f"/mdb/protocols/{assay_prot_id}")

    def find_protocol_by_name(self, name: str) -> dict[str, Any] | None:
        """GET /mdb/protocols?name=<name> — find by exact ProtName."""
        return self._request("GET", f"/mdb/protocols?name={quote(name)}")

    def get_max_protocol_id(self) -> int:
        """GET /mdb/max-protocol-id — highest AssayProtID."""
        result = self._request("GET", "/mdb/max-protocol-id")
        if result is None:
            return 0
        return result.get("max_assay_prot_id", 0)

    def insert_protocol(self, protocol: dict[str, Any]) -> int:
        """POST /mdb/protocols — insert a new AssayProtocol row."""
        result = self._request("POST", "/mdb/protocols", body=protocol)
        if result is None:
            raise VmAgentError("vm-agent returned empty response for insert")
        return result.get("assay_prot_id", protocol.get("AssayProtID", 0))

    def delete_protocol(self, assay_prot_id: int) -> bool:
        """DELETE /mdb/protocols/<id> — delete an AssayProtocol."""
        result = self._request("DELETE", f"/mdb/protocols/{assay_prot_id}")
        if result is None:
            return False
        return result.get("deleted", False)

    def backup_mdb(self, backup_path: str) -> str:
        """POST /mdb/backup — create a timestamped MDB backup.

        The ``backup_path`` argument is a filename (not a full path).
        The vm-agent stores it in its configured backup directory and
        returns the full path.
        """
        result = self._request("POST", "/mdb/backup", body={"name": backup_path})
        if result is None:
            raise VmAgentError("vm-agent returned empty response for backup")
        return result.get("backup_path", "")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """POST /mdb/query — execute a SELECT query (read-only)."""
        # Interpolate params into SQL if provided (simple %s substitution).
        # The vm-agent only allows SELECT queries.
        final_sql = sql % params if params else sql
        result = self._request("POST", "/mdb/query", body={"sql": final_sql})
        if result is None:
            return []
        return result.get("rows", [])
