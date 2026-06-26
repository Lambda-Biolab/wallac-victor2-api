"""Tests for the designer/Run Builder backend (Stage 3).

Tests cover:
- Draft CRUD (create, get, update, list) for all four object kinds
- Finalize (canonicalize + attach + hash)
- Clone from signed object
- Signed object immutability (mutation rejected)
- Auth token enforcement
- BridgeError → HTTPException mapping
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.canonical import canonicalize_and_hash
from bridge.designer import DesignerService
from bridge.designer_app import create_designer_app
from bridge.errors import OPERATOR_REVIEW_REQUIRED, BridgeError
from bridge.schemas import LifecycleState

# --- Mock eLabFTW client ---


class MockDesignerClient:
    """In-memory mock implementing the DesignerElabftwClient protocol."""

    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._next_id = 1000
        self._uploads: dict[int, list[dict[str, Any]]] = {}
        self._upload_data: dict[tuple[int, int], bytes] = {}
        self._next_upload_id = 5000

    def list_items(self, category_id: int) -> list[dict[str, Any]]:
        return [v for v in self._items.values() if v.get("category") == category_id]

    def get_item(self, item_id: int) -> dict[str, Any]:
        if item_id not in self._items:
            raise KeyError(f"Item {item_id} not found")
        return dict(self._items[item_id])

    def create_item(self, category_id: int, title: str, body: str = "") -> int:
        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = {
            "id": item_id,
            "title": title,
            "body": body,
            "category": category_id,
            "metadata": None,
        }
        self._uploads[item_id] = []
        return item_id

    def patch_item(self, item_id: int, fields: dict[str, Any]) -> None:
        self._items[item_id].update(fields)

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        item = self._items[item_id]
        meta = item.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta)
        if not isinstance(meta, dict):
            meta = {}
        ef = meta.get("extra_fields") or {}
        ef.update(extra_fields)
        meta["extra_fields"] = ef
        item["metadata"] = json.dumps(meta)

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        upload_id = self._next_upload_id
        self._next_upload_id += 1
        upload = {"id": upload_id, "real_name": filename, "comment": comment}
        self._uploads.setdefault(item_id, []).append(upload)
        self._upload_data[(item_id, upload_id)] = content
        return upload

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        return self._uploads.get(item_id, [])

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        return self._upload_data.get((item_id, upload_id), b"")

    # Helper for tests: set lifecycle state on an item
    def set_lifecycle(self, item_id: int, state: str) -> None:
        self.patch_metadata(item_id, {"Lifecycle state": {"value": state}})


# --- Fixtures ---


@pytest.fixture
def mock_client() -> MockDesignerClient:
    return MockDesignerClient()


@pytest.fixture
def service(mock_client: MockDesignerClient) -> DesignerService:
    return DesignerService(mock_client)


@pytest.fixture
def app(service: DesignerService) -> Any:
    return create_designer_app(service=service)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


@pytest.fixture
def authed_app(service: DesignerService, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("WALLAC_DESIGNER_TOKEN", "secret-token")
    return create_designer_app(service=service)


@pytest.fixture
def authed_client(authed_app: Any) -> TestClient:
    return TestClient(authed_app)


# --- Sample specs ---


def sample_method_spec() -> dict[str, Any]:
    return {
        "schema_name": "wallac.method",
        "schema_version": 1,
        "mode": "photometry",
        "name": "OD600 Test Method",
        "plate_type": "96-well",
        "photometry": {
            "filter_id": "P610",
            "filter_name": "610nm",
            "read_time_seconds": 1.0,
        },
    }


def sample_layout_spec() -> dict[str, Any]:
    return {
        "schema_name": "wallac.layout",
        "schema_version": 1,
        "plate_type": "96-well",
        "wells": [
            {"well_name": "A1", "role": "measured", "sample_name": "Sample 1"},
            {"well_name": "A2", "role": "skipped"},
        ],
    }


def sample_analysis_spec() -> dict[str, Any]:
    return {
        "schema_name": "wallac.analysis",
        "schema_version": 1,
        "blank_subtraction": {"enabled": True, "blank_wells": ["H11", "H12"]},
        "replicate_aggregation": {"enabled": True, "group_by": "replicate_group"},
        "normalization": {"enabled": False},
        "thresholds": [],
        "exclusions": [],
        "outputs": ["raw_results", "analyzed_wells", "replicate_summary", "analysis_summary"],
    }


def sample_job_spec() -> dict[str, Any]:
    return {
        "schema_name": "wallac.job",
        "schema_version": 1,
        "execution_mode": "generated_protocol",
        "method": {"object_id": 10, "hash": "abc123", "json_attachment_id": 5001},
        "layout": {
            "source": "reusable",
            "hash": "def456",
            "json_attachment_id": 5002,
            "object_id": 11,
        },
        "analysis": {"object_id": 12, "hash": "ghi789", "json_attachment_id": 5003},
    }


# --- DesignerService tests ---


class TestDesignerServiceCreate:
    def test_create_method_draft(self, service: DesignerService) -> None:
        spec = sample_method_spec()
        draft = service.create_draft("method", "Test Method", spec)
        assert draft.item_id > 0
        assert draft.title == "Test Method"
        assert draft.lifecycle == LifecycleState.DRAFT.value
        assert draft.spec_dict == spec

    def test_create_layout_draft(self, service: DesignerService) -> None:
        spec = sample_layout_spec()
        draft = service.create_draft("layout", "Test Layout", spec)
        assert draft.item_id > 0
        assert draft.lifecycle == LifecycleState.DRAFT.value

    def test_create_analysis_draft(self, service: DesignerService) -> None:
        spec = sample_analysis_spec()
        draft = service.create_draft("analysis", "Test Analysis", spec)
        assert draft.item_id > 0

    def test_create_job_draft(self, service: DesignerService) -> None:
        spec = sample_job_spec()
        draft = service.create_draft("job", "Test Job", spec)
        assert draft.item_id > 0

    def test_create_invalid_kind_raises(self, service: DesignerService) -> None:
        with pytest.raises(ValueError, match="Unknown kind"):
            service.create_draft("invalid", "Test", {})


class TestDesignerServiceGet:
    def test_get_draft(self, service: DesignerService, mock_client: MockDesignerClient) -> None:
        spec = sample_method_spec()
        created = service.create_draft("method", "Test Method", spec)
        retrieved = service.get_draft("method", created.item_id)
        assert retrieved.item_id == created.item_id
        assert retrieved.title == "Test Method"
        assert retrieved.spec_dict == spec

    def test_get_draft_preserves_lifecycle(
        self, service: DesignerService, mock_client: MockDesignerClient
    ) -> None:
        created = service.create_draft("method", "Test", sample_method_spec())
        mock_client.set_lifecycle(created.item_id, LifecycleState.SIGNED_ACTIVE.value)
        retrieved = service.get_draft("method", created.item_id)
        assert retrieved.lifecycle == LifecycleState.SIGNED_ACTIVE.value


class TestDesignerServiceUpdate:
    def test_update_draft(self, service: DesignerService) -> None:
        created = service.create_draft("method", "Test", sample_method_spec())
        new_spec = sample_method_spec()
        new_spec["name"] = "Updated Method"
        updated = service.update_draft("method", created.item_id, new_spec)
        assert updated.spec_dict["name"] == "Updated Method"

    def test_update_signed_rejected(
        self, service: DesignerService, mock_client: MockDesignerClient
    ) -> None:
        created = service.create_draft("method", "Test", sample_method_spec())
        mock_client.set_lifecycle(created.item_id, LifecycleState.SIGNED_ACTIVE.value)
        with pytest.raises(BridgeError) as exc_info:
            service.update_draft("method", created.item_id, sample_method_spec())
        assert exc_info.value.code == OPERATOR_REVIEW_REQUIRED
        assert "immutable" in exc_info.value.human_message.lower()


class TestDesignerServiceList:
    def test_list_drafts(self, service: DesignerService) -> None:
        service.create_draft("method", "Method 1", sample_method_spec())
        service.create_draft("method", "Method 2", sample_method_spec())
        drafts = service.list_drafts("method")
        assert len(drafts) == 2

    def test_list_empty(self, service: DesignerService) -> None:
        drafts = service.list_drafts("method")
        assert drafts == []


class TestDesignerServiceFinalize:
    def test_finalize_draft(
        self, service: DesignerService, mock_client: MockDesignerClient
    ) -> None:
        spec = sample_method_spec()
        created = service.create_draft("method", "Test Method", spec)
        finalized = service.finalize_draft("method", created.item_id)

        assert finalized.hash != ""
        assert finalized.json_attachment_id > 0

        # Verify hash matches canonical bytes
        expected_bytes, expected_hash = canonicalize_and_hash(spec)
        assert finalized.hash == expected_hash

        # Verify upload was created
        uploads = mock_client.list_uploads(created.item_id)
        assert len(uploads) == 1
        assert uploads[0]["real_name"] == "method.json"

        # Verify uploaded bytes match canonical
        downloaded = mock_client.download_upload(created.item_id, finalized.json_attachment_id)
        assert downloaded == expected_bytes

    def test_finalize_signed_rejected(
        self, service: DesignerService, mock_client: MockDesignerClient
    ) -> None:
        created = service.create_draft("method", "Test", sample_method_spec())
        mock_client.set_lifecycle(created.item_id, LifecycleState.SIGNED_ACTIVE.value)
        with pytest.raises(BridgeError) as exc_info:
            service.finalize_draft("method", created.item_id)
        assert exc_info.value.code == OPERATOR_REVIEW_REQUIRED

    def test_finalize_layout(self, service: DesignerService) -> None:
        created = service.create_draft("layout", "Test Layout", sample_layout_spec())
        finalized = service.finalize_draft("layout", created.item_id)
        assert finalized.hash != ""
        assert finalized.json_attachment_id > 0

    def test_finalize_analysis(self, service: DesignerService) -> None:
        created = service.create_draft("analysis", "Test Analysis", sample_analysis_spec())
        finalized = service.finalize_draft("analysis", created.item_id)
        assert finalized.hash != ""

    def test_finalize_job(self, service: DesignerService) -> None:
        created = service.create_draft("job", "Test Job", sample_job_spec())
        finalized = service.finalize_draft("job", created.item_id)
        assert finalized.hash != ""


class TestDesignerServiceClone:
    def test_clone_signed(self, service: DesignerService, mock_client: MockDesignerClient) -> None:
        # Create and finalize a method
        spec = sample_method_spec()
        original = service.create_draft("method", "Original Method", spec)
        service.finalize_draft("method", original.item_id)
        mock_client.set_lifecycle(original.item_id, LifecycleState.SIGNED_ACTIVE.value)

        # Clone it
        clone = service.clone_signed("method", original.item_id, "Cloned Method")
        assert clone.item_id != original.item_id
        assert clone.title == "Cloned Method"
        assert clone.lifecycle == LifecycleState.DRAFT.value
        assert clone.spec_dict == spec

        # Verify lineage fields
        retrieved = service.get_draft("method", clone.item_id)
        assert retrieved.extra_fields.get("Parent object ID", {}).get("value") == str(
            original.item_id
        )

    def test_clone_non_signed_rejected(
        self, service: DesignerService, mock_client: MockDesignerClient
    ) -> None:
        created = service.create_draft("method", "Draft", sample_method_spec())
        # Still in draft state
        with pytest.raises(BridgeError) as exc_info:
            service.clone_signed("method", created.item_id, "Clone")
        assert exc_info.value.code == OPERATOR_REVIEW_REQUIRED


# --- FastAPI app tests ---


class TestDesignerAppHealth:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestDesignerAppAuth:
    def test_no_token_required_by_default(self, client: TestClient) -> None:
        r = client.get("/api/methods")
        assert r.status_code == 200

    def test_token_required_when_set(self, authed_client: TestClient) -> None:
        r = authed_client.get("/api/methods")
        assert r.status_code == 401

    def test_valid_token_passes(self, authed_client: TestClient) -> None:
        r = authed_client.get(
            "/api/methods",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert r.status_code == 200

    def test_invalid_token_rejected(self, authed_client: TestClient) -> None:
        r = authed_client.get(
            "/api/methods",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401


class TestDesignerAppMethods:
    def test_create_and_get_method(self, client: TestClient) -> None:
        r = client.post("/api/methods", json={"title": "Test Method", "spec": sample_method_spec()})
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Test Method"
        assert data["lifecycle"] == "draft"
        item_id = data["item_id"]

        r2 = client.get(f"/api/methods/{item_id}")
        assert r2.status_code == 200
        assert r2.json()["item_id"] == item_id

    def test_list_methods(self, client: TestClient) -> None:
        client.post("/api/methods", json={"title": "M1", "spec": sample_method_spec()})
        client.post("/api/methods", json={"title": "M2", "spec": sample_method_spec()})
        r = client.get("/api/methods")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_update_method(self, client: TestClient) -> None:
        r = client.post("/api/methods", json={"title": "Test", "spec": sample_method_spec()})
        item_id = r.json()["item_id"]
        new_spec = sample_method_spec()
        new_spec["name"] = "Updated"
        r2 = client.patch(f"/api/methods/{item_id}", json={"spec": new_spec})
        assert r2.status_code == 200
        assert r2.json()["spec"]["name"] == "Updated"

    def test_finalize_method(self, client: TestClient) -> None:
        r = client.post("/api/methods", json={"title": "Test", "spec": sample_method_spec()})
        item_id = r.json()["item_id"]
        r2 = client.post(f"/api/methods/{item_id}/finalize")
        assert r2.status_code == 200
        data = r2.json()
        assert data["hash"] != ""
        assert data["json_attachment_id"] > 0
        assert data["filename"] == "method.json"


class TestDesignerAppLayouts:
    def test_create_and_finalize_layout(self, client: TestClient) -> None:
        r = client.post("/api/layouts", json={"title": "Test Layout", "spec": sample_layout_spec()})
        assert r.status_code == 200
        item_id = r.json()["item_id"]

        r2 = client.post(f"/api/layouts/{item_id}/finalize")
        assert r2.status_code == 200
        assert r2.json()["filename"] == "layout.json"


class TestDesignerAppAnalyses:
    def test_create_and_finalize_analysis(self, client: TestClient) -> None:
        r = client.post("/api/analyses", json={"title": "Test", "spec": sample_analysis_spec()})
        assert r.status_code == 200
        item_id = r.json()["item_id"]

        r2 = client.post(f"/api/analyses/{item_id}/finalize")
        assert r2.status_code == 200
        assert r2.json()["filename"] == "analysis.json"


class TestDesignerAppJobs:
    def test_create_and_finalize_job(self, client: TestClient) -> None:
        r = client.post("/api/jobs", json={"title": "Test Job", "spec": sample_job_spec()})
        assert r.status_code == 200
        item_id = r.json()["item_id"]

        r2 = client.post(f"/api/jobs/{item_id}/finalize")
        assert r2.status_code == 200
        assert r2.json()["filename"] == "job.json"


class TestDesignerAppErrorMapping:
    def test_signed_mutation_returns_409(
        self, client: TestClient, mock_client: MockDesignerClient
    ) -> None:
        r = client.post("/api/methods", json={"title": "Test", "spec": sample_method_spec()})
        item_id = r.json()["item_id"]
        mock_client.set_lifecycle(item_id, LifecycleState.SIGNED_ACTIVE.value)

        r2 = client.patch(f"/api/methods/{item_id}", json={"spec": sample_method_spec()})
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail["code"] == "operator_review_required"
