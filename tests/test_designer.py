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
import logging
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

    def list_items(self, category_id: int, expected_schema: str = "") -> list[dict[str, Any]]:
        items = [v for v in self._items.values() if v.get("category") == category_id]
        if expected_schema:
            filtered = []
            for item in items:
                meta = item.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                if not isinstance(meta, dict):
                    continue
                ef = meta.get("extra_fields", {})
                ds = ef.get("Designer spec", {})
                spec_json = ds.get("value", "") if isinstance(ds, dict) else ""
                if spec_json:
                    try:
                        spec = json.loads(spec_json)
                        if spec.get("schema_name") == expected_schema:
                            filtered.append(item)
                    except Exception:
                        # Reason: best-effort parse of a draft's Designer-spec
                        # JSON in the mock client; malformed JSON is simply
                        # skipped from the filtered result.
                        logging.getLogger(__name__).debug(
                            "Mock designer spec parse failed", exc_info=True
                        )
            return filtered
        return items

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


# --- Defense-in-depth: /config auth + SSRF hardening (2026-07) -----------


class TestDesignerConfigEndpoint:
    """The /config endpoint must be behind auth and must not leak
    internal URLs (vm_agent_url was the libvirt NAT address)."""

    def test_config_requires_auth_when_token_set(self, authed_client: TestClient) -> None:
        r = authed_client.get("/config")
        # Without bearer token: 401.
        assert r.status_code == 401

    def test_config_returns_urls_with_auth(self, authed_client: TestClient) -> None:
        r = authed_client.get("/config", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        body = r.json()
        # vm_agent_url is now an internal-only field and must not leak.
        assert "vm_agent_url" not in body
        # elabftw_url and bridge_url are still returned for the SPA.
        assert "elabftw_url" in body
        assert "bridge_url" in body


class TestRunBuilderConfigAuth:
    """The Run Builder SPA fetches /config at load time to auto-fill URLs.

    /config is behind the same bearer-token check as the rest of the
    designer API, so the SPA's autoConfig() fetch must send the saved
    token via authHeaders(). Without it, token-enabled deployments
    silently 401 on /config and the Run Builder cannot auto-configure.

    These are source-level checks against the served HTML, so they
    run without a browser, matching the existing route-behavioral
    test patterns in this file.
    """

    def test_run_builder_served(self, client: TestClient) -> None:
        r = client.get("/run-builder")
        assert r.status_code == 200
        assert "autoConfig" in r.text

    def test_autoconfig_sends_auth_headers(self, client: TestClient) -> None:
        """autoConfig() must call fetch('/config', { headers: authHeaders() })."""
        html = client.get("/run-builder").text
        # Reason: a literal ``fetch('/config')`` (no headers) is the original
        # regression — the SPA 401s whenever WALLAC_DESIGNER_TOKEN is set and
        # auto-configure silently fails. Assert both a positive engagement
        # with authHeaders and the absence of a bare /config fetch.
        assert "fetch('/config', { headers: authHeaders() })" in html, (
            "autoConfig() must send saved bearer auth on /config"
        )
        assert "fetch('/config')" not in html, (
            "bare /config fetch without authHeaders() must not be present"
        )

    def test_save_settings_retries_autoconfig(self, client: TestClient) -> None:
        """saveSettings() must call autoConfig() after persisting the token.

        Reason: the first page load fetches authenticated /config before a
        token exists and silently 401s, so URLs are never auto-filled. If
        saveSettings only stores the token without retrying autoConfig, the
        operator is forced to reload or type the eLabFTW/bridge URLs by
        hand. autoConfig's empty-value guards (`if (!elabftwUrl && ...)`)
        ensure explicit user values just typed into the form are not
        overwritten, and autoConfig never calls saveSettings, so there is
        no recursion.
        """
        html = client.get("/run-builder").text
        save_idx = html.find("function saveSettings()")
        cfg_idx = html.find("function autoConfig()")
        assert save_idx != -1 and cfg_idx != -1, "saveSettings/autoConfig missing"
        next_fn = html.find("function ", save_idx + 1)
        save_body = html[save_idx : next_fn if next_fn != -1 else save_idx + 1024]
        # saveSettings body must invoke autoConfig (retry after token saved).
        assert "autoConfig()" in save_body, (
            "saveSettings() must call autoConfig() so a freshly-saved token "
            "retries the /config auto-fill instead of forcing a reload"
        )
        # And it must not re-declare the function (avoid swallow the retry).
        assert "function autoConfig" not in save_body, (
            "saveSettings() must call autoConfig(), not shadow it"
        )


class TestRunBuilderXSSSinks:
    """Stored/DOM-XSS regression checks for bridge/run_builder.html.

    The Run Builder SPA interpolates untrusted data (eLabFTW booking
    user/title, saved method/layout spec fields, bridge job event
    details/errors, server error messages) into ``innerHTML`` template
    strings and double-quoted attributes. Each sink must route through a
    central ``escHtml()`` helper (or use ``textContent``) so an attacker
    controlling those values cannot inject HTML/JS in the browser.

    These are source-level checks against the served HTML, so they run
    without a browser, matching the existing source/served-HTML test
    patterns in this file (see ``TestRunBuilderConfigAuth``).
    """

    HTML = ""  # populated per-test via the client fixture

    @staticmethod
    def _html(client: TestClient) -> str:
        return client.get("/run-builder").text

    def test_esc_html_helper_defined(self, client: TestClient) -> None:
        """A single central escape helper exists and escapes all 5 chars."""
        html = self._html(client)
        assert "function escHtml(s)" in html, "central escHtml() helper missing"
        body_start = html.find("function escHtml(s)")
        body_end = html.find("}", html.find(".replace(/'/g,", body_start) + 1)
        body = html[body_start:body_end]
        # Reason: every character that breaks out of text content or a
        # "..."/'...' attribute must be encoded; missing one re-opens XSS.
        for needle in (
            ".replace(/&/g",
            ".replace(/</g",
            ".replace(/>/g",
            '.replace(/"/g',
            ".replace(/'/g",
        ):
            assert needle in body, f"escHtml() does not escape {needle!r}"

    def test_show_status_escapes_msg_and_type(self, client: TestClient) -> None:
        """showStatus() interpolates msg (carries server e.message) and a
        CSS type into innerHTML — both must be escaped."""
        html = self._html(client)
        assert 'status-msg ${escHtml(type)}">${escHtml(msg)}' in html
        assert 'status-msg ${type}">${msg}' not in html

    def test_booking_banner_escapes_booker(self, client: TestClient) -> None:
        """eLabFTW booking fullname/title_only (booker) must be escaped
        before interpolation into bookingText.innerHTML, in both the
        active-booking and upcoming-booking branches."""
        html = self._html(client)
        assert "escHtml(active.fullname" in html
        assert "escHtml(upcoming.fullname" in html
        # Rendered copy must be preserved (not stripped to fix the bug).
        assert "Instrument is available." in html
        # booker must be the *already-escaped* string when assigned so the
        # raw ${booker} interpolation in the template is safe.
        assert "escHtml(active.fullname || active.title_only" in html
        assert "escHtml(upcoming.fullname || upcoming.title_only" in html
        # And the raw fullname must no longer flow straight into innerHTML.
        assert "${active.fullname || active.title_only}</strong>" not in html
        assert "${upcoming.fullname || upcoming.title_only}</strong>" not in html

    def test_booking_banner_escapes_calendar_href(self, client: TestClient) -> None:
        """Calendar links require an escaped, http(s)-validated URL."""
        html = self._html(client)
        assert "const calUrl = safeHttpUrl" in html
        assert 'href="${escHtml(calUrl)}"' in html
        assert 'rel="noopener noreferrer"' in html
        assert 'href="${elabftwUrl}/database.php' not in html

    def test_load_methods_escapes_titles_and_spec_fields(self, client: TestClient) -> None:
        """Saved method title/lifecycle/spec.mode are server/user strings
        interpolated into methodList.innerHTML — must be escaped. The
        inline onclick JS arg must be coerced to Number to block JS-arg
        injection from a crafted item_id."""
        html = self._html(client)
        assert 'resource-title">${escHtml(m.title)}' in html
        assert 'resource-title">${m.title}' not in html
        assert "lifecycle=${escHtml(m.lifecycle)}" in html
        assert "mode=${escHtml(m.spec.mode||'?')}" in html
        assert "selectMethod(${Number(m.item_id)})" in html
        assert "selectMethod(${m.item_id})" not in html

    def test_sample_legend_escapes_user_names(self, client: TestClient) -> None:
        """sample_name / replicate_group are user-input text saved on the
        layout spec; both legends must escape them before innerHTML."""
        html = self._html(client)
        assert "></div>${escHtml(name)}</div>" in html
        assert "></div>${name}</div>" not in html

    def test_select_well_escapes_input_attributes(self, client: TestClient) -> None:
        """selectWell() interpolates sample_name / replicate_group into
        double-quoted value="" attributes — must be escaped to prevent
        attribute breakout (stored XSS via saved layout)."""
        html = self._html(client)
        assert "value=\"${escHtml(w.sample_name||'')}\"" in html
        assert "value=\"${escHtml(w.replicate_group||'')}\"" in html
        assert "value=\"${w.sample_name||''}\"" not in html
        assert "value=\"${w.replicate_group||''}\"" not in html

    def test_job_events_log_escapes_bridge_fields(self, client: TestClient) -> None:
        """Bridge job events (ts/event/detail, incl. error strings) flow
        into statusEvents.innerHTML — each field must be escaped."""
        html = self._html(client)
        assert "${escHtml(e.ts.slice(0,19))}] ${escHtml(e.event)}: ${escHtml(e.detail)}" in html
        assert "${e.ts.slice(0,19)}] ${e.event}: ${e.detail}" not in html

    def test_result_link_escapes_href(self, client: TestClient) -> None:
        """Result links require an escaped, http(s)-validated URL."""
        html = self._html(client)
        assert "const expUrl = safeHttpUrl" in html
        assert 'href="${escHtml(expUrl)}"' in html
        assert 'href="${expUrl}"' not in html

    def test_external_links_allow_only_http_schemes(self, client: TestClient) -> None:
        html = self._html(client)
        assert "function safeHttpUrl(value)" in html
        assert "new URL(String(value))" in html
        assert "['http:', 'https:'].includes(url.protocol)" in html
        assert "javascript:" not in html
        assert "data:" not in html

    def test_textcontent_sinks_left_intact(self, client: TestClient) -> None:
        """Sinks that already use textContent (instrument detail, status
        error, progress label, booking error branch) must remain
        textContent — they were never XSS sinks and must not regress."""
        html = self._html(client)
        # instrumentDetail uses textContent for currentJob; statusError for
        # job.error; progressFill.textContent for the progress label.
        assert "detailEl.textContent =" in html
        assert "document.getElementById('statusError').textContent" in html
        assert "progressFill.textContent = prog.label" in html
        # booking error branch already used textContent for the error msg.
        assert "bookingText').textContent = `Could not check booking status" in html


class TestDesignerElabftwEventsSSRF:
    """The /elabftw/events proxy must percent-encode user-supplied
    query parameters so they cannot inject additional parameters or
    path segments into the eLabFTW URL."""

    def test_url_encodes_injected_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """start='2026-01-01#evil&host=bad' is URL-encoded, not raw."""
        from bridge.config import BridgeConfig
        from bridge.designer_app import create_designer_app

        captured: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"[]"

        def fake_urlopen(req, context=None):
            captured["url"] = req.full_url
            return FakeResponse()

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        # Build a config so the proxy has a base URL to use.
        config = BridgeConfig.from_env(
            env={
                "WALLAC_ELABFTW_API_KEY": "5-key",
                "WALLAC_ELABFTW_URL": "https://elab.local:3148",
            }
        )
        # Build with a real config (the proxy will use config.elabftw_url).
        app = create_designer_app(config=config, service=object())  # type: ignore[arg-type]
        with TestClient(app) as client:
            client.get(
                "/elabftw/events",
                params={
                    "items_id": "42",
                    "start": "2026-01-01#evil&host=bad",
                    "end": "2026-12-31",
                },
            )

        # We just need to confirm the URL was URL-encoded.
        assert captured.get("url"), "urlopen was not called"
        url = captured["url"]
        # The literal '#' must be encoded as %23, '&' as %26.
        assert "#evil" not in url, f"raw '#' leaked into URL: {url}"
        assert "host=bad" not in url, f"raw 'host=bad' leaked into URL: {url}"
        # Confirm the host is still the configured eLabFTW (not a redirect).
        assert url.startswith("https://elab.local:3148/api/v2/events?")
        # The encoded payload is present.
        assert "items_id=42" in url
        assert "%26" in url or "%23" in url
