"""eLabFTW API v2 client for the direct-submit bridge and Run Builder.

Handles canonical attachment downloads, designer item CRUD, and assay result
write-back. Metadata may be double-encoded JSON, so helpers normalize it before
reading or updating ``extra_fields``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

from bridge.config import ConfigError

logger = logging.getLogger(__name__)


def build_ssl_context(*, verify_tls: bool, ca_bundle: str | None = None) -> ssl.SSLContext:
    """Build the eLabFTW trust context without weakening normal verification."""
    # Reason: direct callers still receive the CA-bundle invariant even when
    # they bypass BridgeConfig.from_env; environment gating remains there.
    if ca_bundle and not verify_tls:
        raise ConfigError("CA bundle cannot be used when TLS verification is disabled")
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ctx = ssl.create_default_context()
    if ca_bundle:
        # Reason: validate in an empty store so a CA already present in system
        # trust is not deduplicated and falsely rejected as a non-CA bundle.
        validation_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        invalid_bundle_error = f"Invalid eLabFTW CA bundle: {ca_bundle}"
        try:
            validation_ctx.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(invalid_bundle_error) from exc
        if validation_ctx.cert_store_stats()["x509_ca"] == 0:
            raise ConfigError(f"eLabFTW CA bundle contains no CA:TRUE trust anchor: {ca_bundle}")
        try:
            # Defensive second read catches replacement/removal between validation and use.
            ctx.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(invalid_bundle_error) from exc
    return ctx


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


# --- HTTP client ------------------------------------------------------------


class ElabftwClient:
    """HTTP client for eLabFTW API v2.

    Uses urllib from the standard library and exposes only operations used by
    the direct-submit bridge and Run Builder.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base = base_url.rstrip("/") + "/api/v2"
        self.api_key = api_key
        self.timeout = timeout
        self._ssl_ctx = build_ssl_context(
            verify_tls=verify_tls,
            ca_bundle=ca_bundle,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310  # Base URL is operator config.
            url, data=data, method=method
        )
        req.add_header("Authorization", self.api_key)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(  # noqa: S310  # Base URL is operator config.
                req, context=self._ssl_ctx, timeout=timeout or self.timeout
            ) as resp:
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

    def check_connection(self, timeout: float | None = None) -> Any:
        """Verify that eLabFTW is reachable and the API key is authorized."""
        return self._request("GET", "/experiments?limit=1&scope=1", timeout=timeout)

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        """Download the raw bytes of an upload attachment.

        Uses ``?format=binary`` because the default response is JSON metadata.
        """
        url = f"{self.base}/items/{item_id}/uploads/{upload_id}?format=binary"
        req = urllib.request.Request(url)  # noqa: S310  # Base URL is operator config.
        req.add_header("Authorization", self.api_key)
        with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=self.timeout) as resp:  # noqa: S310
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
                "metadata": json.dumps(meta, ensure_ascii=False),
            },
        )

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        """Upload a file attachment via multipart/form-data."""
        import uuid

        boundary = uuid.uuid4().hex
        body_parts: list[bytes] = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        body_parts.append(content)
        body_parts.append(f"\r\n--{boundary}\r\n".encode())
        body_parts.append(
            (f'Content-Disposition: form-data; name="comment"\r\n\r\n{comment}\r\n').encode()
        )
        body_parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(body_parts)

        url = f"{self.base}/items/{item_id}/uploads"
        req = urllib.request.Request(  # noqa: S310  # Base URL is operator config.
            url, data=data, method="POST"
        )
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=self.timeout) as resp:  # noqa: S310
                content_resp = resp.read()
                if content_resp:
                    return json.loads(content_resp)
                # 201 with Location header but empty body
                loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                if loc:
                    try:
                        upload_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
                        return {"id": upload_id, "real_name": filename}
                    except ValueError:
                        pass
                return {}
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:200]
            logger.error("eLabFTW upload %s -> %s: %s", url, e.code, detail)
            raise

    # --- Designer methods (Stage 3: protocol authoring) ---

    def list_items(self, category_id: int, expected_schema: str = "") -> list[dict[str, Any]]:
        """List all items created from a resource template.

        eLabFTW's ``?type=`` filter returns all items, not just those from
        the specified template. We filter client-side by checking for the
        ``Designer spec`` metadata field that the designer writes when
        creating drafts. If ``expected_schema`` is provided, also filter
        by the ``schema_name`` field in the spec to prevent cross-contamination
        (e.g. job items appearing in the methods list).
        """
        all_items = self._request("GET", f"/items?type={category_id}") or []
        result = []
        for item in all_items:
            if not self._item_has_designer_spec(item):
                continue
            if expected_schema and not self._item_matches_schema(item, expected_schema):
                continue
            result.append(item)
        return result

    @staticmethod
    def _item_has_designer_spec(item: dict[str, Any]) -> bool:
        """Return True if the item has a 'Designer spec' extra_field."""
        meta = normalize_metadata(item.get("metadata"))
        if meta is None:
            return False
        ef = meta.get("extra_fields") or {}
        return "Designer spec" in ef

    @staticmethod
    def _item_matches_schema(item: dict[str, Any], expected_schema: str) -> bool:
        """Return True if the item's Designer spec matches the schema name.

        On parse failure (corrupt JSON, missing field), returns False so the
        caller can ``continue`` past the item without polluting downstream
        filter logic.
        """
        meta = normalize_metadata(item.get("metadata"))
        if meta is None:
            return False
        ef = meta.get("extra_fields") or {}
        ds = ef.get("Designer spec")
        spec_json = ds.get("value", "") if isinstance(ds, dict) else ""
        if not spec_json:
            return False
        try:
            spec = json.loads(spec_json)
        except (json.JSONDecodeError, TypeError):
            return False
        return spec.get("schema_name") == expected_schema

    def get_item(self, item_id: int) -> dict[str, Any]:
        """Get a single item by ID."""
        return self._request("GET", f"/items/{item_id}")

    def create_item(self, category_id: int, title: str, body: str = "") -> int:
        """Create a new item from a resource template. Returns the new item ID.

        Uses ``type`` (the items_types template ID) to create the item from
        the template, then PATCHes the ``category`` field to link it to the
        correct items_categories entry. This is needed because eLabFTW's API
        creates the item from the template but doesn't set the category field.
        """
        result = self._request(
            "POST",
            "/items",
            body={"type": category_id, "title": title, "body": body},
        )
        if isinstance(result, dict):
            item_id = None
            if "id" in result:
                item_id = int(result["id"])
            elif "_location" in result:
                loc = result["_location"]
                with contextlib.suppress(ValueError):
                    item_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
            if item_id is not None:
                # Set the category field so the item appears in the UI.
                # This is best-effort: if the PATCH fails (e.g., because the
                # items_types ID doesn't map to an items_categories ID), the
                # item is still created and functional — category is only
                # for UI grouping.
                try:
                    self._request(
                        "PATCH",
                        f"/items/{item_id}",
                        body={"category": category_id},
                    )
                except Exception:
                    logger.warning(
                        "Failed to set category=%s on item %d (non-fatal)",
                        category_id,
                        item_id,
                    )
                return item_id
        raise RuntimeError(f"Could not parse new item ID from response: {result}")

    # --- Experiment methods (Stage 6: Assay summary write-back) ---

    def create_experiment(self, title: str, body: str = "") -> int:
        """Create a new experiment. Returns the new experiment ID.

        If ``body`` is provided, it is set as the experiment body (HTML).
        """
        result = self._request("POST", "/experiments", body={"title": title, "body": body})
        if isinstance(result, dict):
            if "id" in result:
                return int(result["id"])
            if "_location" in result:
                loc = result["_location"]
                with contextlib.suppress(ValueError):
                    return int(loc.rstrip("/").rsplit("/", 1)[-1])
        raise RuntimeError(f"Could not parse new experiment ID from response: {result}")

    def patch_experiment(self, experiment_id: int, fields: dict[str, Any]) -> None:
        """Patch fields on an experiment (title, body, etc.)."""
        self._request("PATCH", f"/experiments/{experiment_id}", body=fields)

    def upload_experiment_file(
        self, experiment_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        """Upload a file attachment to an experiment."""
        import uuid

        boundary = uuid.uuid4().hex
        body_parts: list[bytes] = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        body_parts.append(content)
        body_parts.append(f"\r\n--{boundary}\r\n".encode())
        body_parts.append(
            (f'Content-Disposition: form-data; name="comment"\r\n\r\n{comment}\r\n').encode()
        )
        body_parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(body_parts)

        url = f"{self.base}/experiments/{experiment_id}/uploads"
        req = urllib.request.Request(  # noqa: S310  # Base URL is operator config.
            url, data=data, method="POST"
        )
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=self.timeout) as resp:  # noqa: S310
                content_resp = resp.read()
                if content_resp:
                    return json.loads(content_resp)
                loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                if loc:
                    try:
                        upload_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
                        return {"id": upload_id, "real_name": filename}
                    except ValueError:
                        pass
                return {}
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode()[:200]
            logger.error("eLabFTW experiment upload %s -> %s: %s", url, e.code, detail)
            raise
