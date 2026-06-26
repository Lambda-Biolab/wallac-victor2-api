"""eLabFTW API v2 client for the Wallac bridge.

Reads Automation Job resources, downloads signature archives, and writes back
state/progress/result fields.  Uses the same API conventions documented in
eLabFTW-lambdabiolab/AGENT_LEARNINGS.md (metadata may be double-encoded JSON,
extra_fields need raw HTTP GET to deserialize, etc.).

The :class:`ElabftwInterface` protocol defines the surface that
:class:`~bridge.intake.JobIntake` depends on.  Tests provide a mock
implementation; production uses :class:`ElabftwClient` over HTTP.
"""

from __future__ import annotations

import contextlib
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import AutomationJob, RequestFields

logger = logging.getLogger(__name__)


# --- Metadata helpers (shared by real and mock clients) --------------------


def normalize_metadata(raw: Any) -> dict[str, Any] | None:
    """Parse metadata from an API response, handling double-encoded JSON.

    The eLabFTW API may return metadata as a JSON string that itself contains
    another JSON string (double-encoding).  This function keeps parsing until
    it gets a dict or gives up.
    """
    result = raw
    while isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None
    return result if isinstance(result, dict) else None


def extract_extra_fields(metadata_raw: Any) -> dict[str, Any]:
    """Extract extra_fields dict from an item's metadata (any encoding)."""
    meta = normalize_metadata(metadata_raw)
    if meta is None:
        return {}
    return meta.get("extra_fields") or {}


def get_field_value(extra_fields: dict[str, Any], name: str) -> str:
    """Read the ``value`` of a named extra_fields entry."""
    entry = extra_fields.get(name)
    if isinstance(entry, dict):
        return str(entry.get("value", ""))
    if entry is None:
        return ""
    return str(entry)


# --- Interface --------------------------------------------------------------


class ElabftwInterface(Protocol):
    """Interface for eLabFTW API access (real HTTP client or mock)."""

    def list_automation_jobs(self) -> list[AutomationJob]:
        """Return all Automation Job items in the configured category."""
        ...

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        """Return uploads (attachments) for an item."""
        ...

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        """Download the raw bytes of an upload."""
        ...

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        """Update extra_fields metadata on an item.

        ``extra_fields`` is a dict of field_name -> field_def where field_def
        has at least ``{"value": ...}``.  Only the provided fields are updated;
        other fields are left unchanged.
        """
        ...

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        """Upload a file attachment to an item.

        Returns the upload metadata dict (with at least ``id`` and
        ``real_name``).
        """
        ...

    def post_comment(self, item_id: int, comment: str) -> None:
        """Append a comment to an item (used for event log entries)."""
        ...


# --- HTTP client ------------------------------------------------------------


class ElabftwClient:
    """HTTP client for eLabFTW API v2.

    Uses urllib (stdlib) so the bridge core has no hard third-party dependency
    beyond PyNaCl (for signature verification).  The mock test client
    implements the same :class:`ElabftwInterface` protocol.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        automation_job_category: int = 9,
    ) -> None:
        self.base = base_url.rstrip("/") + "/api/v2"
        self.api_key = api_key
        self.category = automation_job_category
        if verify_tls:
            self._ssl_ctx = ssl.create_default_context()
        else:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.api_key)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx) as resp:
                content = resp.read()
                if not content:
                    # POST may return 201 with Location header but empty body.
                    # Return a dict with the Location so callers can parse the ID.
                    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                    if loc:
                        return {"_location": loc}
                    return None
                return json.loads(content)
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:200]
            logger.error("eLabFTW API %s %s -> %s: %s", method, path, e.code, detail)
            raise

    # --- ElabftwInterface implementation ---

    def list_automation_jobs(self) -> list[AutomationJob]:
        items = self._request("GET", f"/items?cat={self.category}")
        jobs: list[AutomationJob] = []
        for item in items or []:
            extra_fields = extract_extra_fields(item.get("metadata"))
            state = get_field_value(extra_fields, "Automation state")
            request_fields = RequestFields.from_extra_fields(extra_fields)
            jobs.append(
                AutomationJob(
                    item_id=item["id"],
                    title=item.get("title", ""),
                    state=state,
                    request_fields=request_fields,
                    extra_fields=extra_fields,
                )
            )
        return jobs

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/items/{item_id}/uploads") or []

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        url = f"{self.base}/items/{item_id}/uploads/{upload_id}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", self.api_key)
        with urllib.request.urlopen(req, context=self._ssl_ctx) as resp:
            return resp.read()

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        # Read current metadata, merge the new fields, and write back.
        # eLabFTW PATCH requires the full metadata JSON string.
        item = self._request("GET", f"/items/{item_id}")
        meta = normalize_metadata(item.get("metadata")) or {}
        current_ef = meta.get("extra_fields") or {}
        current_ef.update(extra_fields)
        meta["extra_fields"] = current_ef
        self._request(
            "PATCH",
            f"/items/{item_id}",
            body={
                "action": "update",
                "metadata": json.dumps(meta, ensure_ascii=False),
            },
        )

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        """Upload a file attachment via multipart/form-data."""
        import uuid

        boundary = uuid.uuid4().hex
        body_parts = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body_parts.append(content)
        body_parts.append(f"\r\n--{boundary}\r\n".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="comment"\r\n\r\n{comment}\r\n'
        ).encode()
        body_parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(body_parts)

        url = f"{self.base}/items/{item_id}/uploads"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx) as resp:
                content_resp = resp.read()
                if content_resp:
                    return json.loads(content_resp)
                return {}
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:200]
            logger.error("eLabFTW upload %s -> %s: %s", url, e.code, detail)
            raise

    def post_comment(self, item_id: int, comment: str) -> None:
        """Append a comment to an item."""
        self._request(
            "POST",
            f"/items/{item_id}/comments",
            body={"comment": comment},
        )

    # --- Designer methods (Stage 3: protocol authoring) ---

    def list_items(self, category_id: int) -> list[dict[str, Any]]:
        """List all items created from a resource template.

        Uses ``?type=`` because items created via the API with ``type`` may
        not have ``category`` set in the ``items_categories`` table.
        """
        return self._request("GET", f"/items?type={category_id}") or []

    def get_item(self, item_id: int) -> dict[str, Any]:
        """Get a single item by ID."""
        return self._request("GET", f"/items/{item_id}")

    def create_item(self, category_id: int, title: str, body: str = "") -> int:
        """Create a new item from a resource template. Returns the new item ID.

        Uses ``type`` (not ``category``) because eLabFTW's ``items_types`` API
        creates templates but doesn't always create corresponding
        ``items_categories`` entries. The ``type`` field tells eLabFTW to
        create the item from the template, which handles the category linkage
        internally.
        """
        result = self._request(
            "POST",
            "/items",
            body={"type": category_id, "title": title, "body": body},
        )
        if isinstance(result, dict):
            if "id" in result:
                return int(result["id"])
            if "_location" in result:
                loc = result["_location"]
                try:
                    return int(loc.rstrip("/").rsplit("/", 1)[-1])
                except ValueError:
                    pass
        raise RuntimeError(f"Could not parse new item ID from response: {result}")

    def patch_item(self, item_id: int, fields: dict[str, Any]) -> None:
        """Patch fields on an item (title, body, etc.)."""
        self._request("PATCH", f"/items/{item_id}", body=fields)
