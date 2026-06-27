"""Tests for RemoteMdbClient — HTTP implementation of the MdbClient Protocol.

Tests cover:
- All MdbClient methods: success, 404, 500, empty-response paths
- HTTP error translation (HTTPError → VmAgentError / None / False / 0)
- URLError (unreachable) → VmAgentError
- SQL interpolation for query()
- Authorization and Content-Type header behaviour
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bridge.remote_mdb_client import RemoteMdbClient
from bridge.vm_agent_client import VmAgentError

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mock_response_body(data: dict[str, Any] | None) -> MagicMock:
    """Create a MagicMock whose .read() returns JSON bytes (or empty bytes)."""
    mock_resp = MagicMock()
    if data is not None:
        mock_resp.read.return_value = json.dumps(data).encode()
    else:
        mock_resp.read.return_value = b""
    return mock_resp


def _setup_urlopen(
    mock_urlopen: MagicMock,
    data: dict[str, Any] | None = None,
) -> MagicMock:
    """Configure mock_urlopen to return a response with the given data."""
    mock_resp = _mock_response_body(data)
    mock_urlopen.return_value.__enter__.return_value = mock_resp
    return mock_resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> RemoteMdbClient:
    return RemoteMdbClient("http://localhost:8420", token="test-token")


# ---------------------------------------------------------------------------
# HTTP error helpers — create realistic urllib.error.HTTPError instances
# ---------------------------------------------------------------------------


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    """Build an HTTPError for side_effect usage in mock."""
    fp = BytesIO(body.encode()) if body else None
    return urllib.error.HTTPError(
        "http://localhost:8420/mdb/test",
        code,
        "Error",
        {},  # headers
        fp,  # fp — None is OK for 404 (read never called); use BytesIO for 500
    )


# ===================================================================
# URL encoding / request shape verification
# ===================================================================


class TestRequestShape:
    """Verify correct HTTP method, URL, and headers are sent."""

    @patch("bridge.remote_mdb_client.urllib.request.urlopen")
    def test_get_request_includes_auth_header(
        self, mock_urlopen: MagicMock, client: RemoteMdbClient
    ) -> None:
        _setup_urlopen(mock_urlopen, {"group_id": 5, "name": "eLabFTW Generated"})

        client.get_protocol_group_id("eLabFTW Generated")

        # Inspect the Request object passed to urlopen
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer test-token"

    @patch("bridge.remote_mdb_client.urllib.request.urlopen")
    def test_post_request_includes_content_type(
        self, mock_urlopen: MagicMock, client: RemoteMdbClient
    ) -> None:
        _setup_urlopen(mock_urlopen, {"assay_prot_id": 2000001, "created": True})

        client.insert_protocol({"AssayProtID": 2000001, "ProtName": "Test"})

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"

    @patch("bridge.remote_mdb_client.urllib.request.urlopen")
    def test_query_sql_is_sent_in_body(
        self, mock_urlopen: MagicMock, client: RemoteMdbClient
    ) -> None:
        _setup_urlopen(mock_urlopen, {"count": 0, "rows": []})

        client.query("SELECT * FROM AssayProtocol WHERE ProtName = '%s'", ("Test",))

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert "SELECT * FROM AssayProtocol WHERE ProtName" in body["sql"]


# ===================================================================
# get_protocol_group_id
# ===================================================================


class TestGetProtocolGroupId:
    def test_returns_group_id_on_success(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"group_id": 5, "name": "eLabFTW Generated"})

            result = client.get_protocol_group_id("eLabFTW Generated")

        assert result == 5

    def test_returns_none_on_404(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(404)

            result = client.get_protocol_group_id("MissingGroup")

        assert result is None

    def test_raises_vm_agent_error_on_500(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(500, "Internal Server Error")

            with pytest.raises(VmAgentError) as exc_info:
                client.get_protocol_group_id("Boom")

        assert exc_info.value.status_code == 500
        assert "Internal Server Error" in str(exc_info.value)


# ===================================================================
# get_protocol
# ===================================================================


class TestGetProtocol:
    def test_returns_protocol_dict_on_success(self, client: RemoteMdbClient) -> None:
        protocol = {"AssayProtID": 2000001, "ProtName": "Absorbance 600", "ProtVersion": 2}
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, protocol)

            result = client.get_protocol(2000001)

        assert result == protocol

    def test_returns_none_on_404(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(404)

            result = client.get_protocol(9999999)

        assert result is None

    def test_raises_vm_agent_error_on_500(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(500, "DB connection lost")

            with pytest.raises(VmAgentError) as exc_info:
                client.get_protocol(2000001)

        assert exc_info.value.status_code == 500


# ===================================================================
# find_protocol_by_name
# ===================================================================


class TestFindProtocolByName:
    def test_returns_protocol_dict_on_success(self, client: RemoteMdbClient) -> None:
        protocol = {"AssayProtID": 1000003, "ProtName": "Absorbance 600"}
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, protocol)

            result = client.find_protocol_by_name("Absorbance 600")

        assert result == protocol

    def test_returns_none_on_404(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(404)

            result = client.find_protocol_by_name("Nonexistent")

        assert result is None


# ===================================================================
# get_max_protocol_id
# ===================================================================


class TestGetMaxProtocolId:
    def test_returns_max_id_on_success(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"max_assay_prot_id": 2000001})

            result = client.get_max_protocol_id()

        assert result == 2000001

    def test_returns_zero_on_404(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(404)

            result = client.get_max_protocol_id()

        assert result == 0

    def test_returns_zero_on_empty_response(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, None)  # empty body → None from _request

            result = client.get_max_protocol_id()

        assert result == 0


# ===================================================================
# insert_protocol
# ===================================================================


class TestInsertProtocol:
    def test_returns_assay_prot_id_on_success(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"assay_prot_id": 2000001, "created": True})

            result = client.insert_protocol(
                {"AssayProtID": 2000001, "ProtName": "ELAB-Job-1-abc123de"}
            )

        assert result == 2000001

    def test_raises_vm_agent_error_on_empty_response(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, None)  # empty body

            with pytest.raises(VmAgentError, match="empty response"):
                client.insert_protocol({"AssayProtID": 2000001})


# ===================================================================
# delete_protocol
# ===================================================================


class TestDeleteProtocol:
    def test_returns_true_on_success(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"assay_prot_id": 2000001, "deleted": True})

            result = client.delete_protocol(2000001)

        assert result is True

    def test_returns_false_on_404(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(404)

            result = client.delete_protocol(2000001)

        assert result is False

    def test_returns_false_when_deleted_field_is_false(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"assay_prot_id": 2000001, "deleted": False})

            result = client.delete_protocol(2000001)

        assert result is False


# ===================================================================
# backup_mdb
# ===================================================================


class TestBackupMdb:
    def test_returns_backup_path_on_success(self, client: RemoteMdbClient) -> None:
        backup_path = "C:\\Users\\Public\\mdb_backups\\mlr3_backup_1_1719400000.mdb"
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"backup_path": backup_path, "created": True})

            result = client.backup_mdb("mlr3_backup_1_1719400000.mdb")

        assert result == backup_path

    def test_raises_vm_agent_error_on_empty_response(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, None)

            with pytest.raises(VmAgentError, match="empty response"):
                client.backup_mdb("mlr3_backup_1.mdb")


# ===================================================================
# query
# ===================================================================


class TestQuery:
    def test_returns_rows_on_success(self, client: RemoteMdbClient) -> None:
        rows = [
            {"AssayProtID": 2000001, "ProtName": "ELAB-Job-1-abc123de"},
            {"AssayProtID": 2000002, "ProtName": "ELAB-Job-2-def456ab"},
        ]
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"count": 2, "rows": rows})

            result = client.query("SELECT * FROM AssayProtocol")

        assert result == rows

    def test_returns_empty_list_on_empty_response(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, None)

            result = client.query("SELECT * FROM AssayProtocol")

        assert result == []

    def test_raises_vm_agent_error_on_500(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _http_error(500, "Query failed")

            with pytest.raises(VmAgentError):
                client.query("SELECT * FROM AssayProtocol")

    def test_sql_interpolation_replaces_params(self, client: RemoteMdbClient) -> None:
        """Verify that %s placeholders are replaced with query params."""
        rows = [{"AssayProtID": 2000001, "ProtName": "ELAB-Job-1-abc123de"}]
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"count": 1, "rows": rows})

            client.query(
                "SELECT * FROM AssayProtocol WHERE ProtName = '%s' AND ProtVersion = %s",
                ("ELAB-Job-1-abc123de", 1),
            )

        # Check the SQL in the request body was interpolated
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["sql"] == (
            "SELECT * FROM AssayProtocol WHERE ProtName = 'ELAB-Job-1-abc123de' AND ProtVersion = 1"
        )

    def test_sql_with_params_does_not_alter_empty_params(self, client: RemoteMdbClient) -> None:
        """Empty params tuple is a no-op — SQL is sent as-is."""
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"count": 0, "rows": []})

            client.query("SELECT * FROM AssayProtocol")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["sql"] == "SELECT * FROM AssayProtocol"


# ===================================================================
# URLError (unreachable) handling
# ===================================================================


class TestUrlError:
    def test_raises_vm_agent_error_when_unreachable(self, client: RemoteMdbClient) -> None:
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            with pytest.raises(VmAgentError, match="unreachable"):
                client.get_protocol(2000001)


# ===================================================================
# Token behaviour
# ===================================================================


class TestTokenHandling:
    def test_no_auth_header_when_token_is_empty(self) -> None:
        client_no_auth = RemoteMdbClient("http://localhost:8420", token="")
        with patch("bridge.remote_mdb_client.urllib.request.urlopen") as mock_urlopen:
            _setup_urlopen(mock_urlopen, {"group_id": 5, "name": "Test"})

            client_no_auth.get_protocol_group_id("Test")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None
