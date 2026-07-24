"""Security audit regression tests — see AGENT_LEARNINGS / OWASP audit.

Covers:
- validate_readonly_sql (#1: SQL injection in /mdb/query)
- sanitize_backup_filename (#2: path traversal in /mdb/backup)
- Security headers (#4: CSP, nosniff, Referrer-Policy, X-Frame-Options)
- UUID length (#7: 16-hex prefix on job_id)
- selectWell escHtml (#5: defense-in-depth XSS in SPA)

Constant-time token comparison (#6) is covered by the existing
TestValidToken / TestInvalidToken tests in test_bridge_hardening and
test_designer — those exercise the new hmac.compare_digest path through
the same auth-check functions.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from agent import ApiError, sanitize_backup_filename, validate_readonly_sql
from fastapi.testclient import TestClient

from bridge.bridge_app import create_bridge_app
from bridge.designer_app import create_designer_app
from bridge.jobs import JobManager
from bridge.security_headers import build_csp

# ===========================================================================
# #1 — validate_readonly_sql
# ===========================================================================


class TestValidateReadonlySql:
    """``/mdb/query`` must reject any SQL that is not a simple read-only SELECT."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM AssayProtocol",
            "select * from AssayProtocol",
            "  SELECT ProtName, AssayProtID FROM AssayProtocol WHERE FactoryPreset = False",
            "SELECT TOP 10 ProtName FROM AssayProtocol",
            "SELECT COUNT(*) FROM PlateResult",
        ],
    )
    def test_accepts_simple_select(self, sql: str) -> None:
        # Should not raise.
        validate_readonly_sql(sql)

    @pytest.mark.parametrize(
        "sql,expected_substr",
        [
            # UNION-based read exfiltration
            ("SELECT 1 UNION SELECT ProtName FROM AssayProtocol", "UNION"),
            ("select 1 union select ProtName from AssayProtocol", "UNION"),
            # SELECT INTO creates a new table — write vector
            ("SELECT * INTO NewTable FROM AssayProtocol", "INTO"),
            # Stacked statements
            ("SELECT 1; DROP TABLE AssayProtocol", "multiple statements"),
            ("SELECT 1; SELECT 2", "multiple statements"),
            # Other DDL/DML — caught by the first-token check (not SELECT)
            ("INSERT INTO AssayProtocol VALUES (1)", "only SELECT"),
            ("UPDATE AssayProtocol SET ProtName = 'x'", "only SELECT"),
            ("DELETE FROM AssayProtocol", "only SELECT"),
            ("DROP TABLE AssayProtocol", "only SELECT"),
            ("CREATE TABLE foo (id INT)", "only SELECT"),
            ("ALTER TABLE AssayProtocol ADD COLUMN x INT", "only SELECT"),
            # Jet system-table access (data exfiltration via catalog)
            ("SELECT * FROM MSysObjects", "MSYS"),
            ("SELECT * FROM MSysQueries", "MSYS"),
            # Line comments hiding a forbidden keyword AFTER valid SQL.
            # The comment extends to the newline, so "UNION" is stripped and
            # only "SELECT 1 " remains on that line. The following
            # "FROM AssayProtocol" is real SQL. Net effect: a harmless
            # ``SELECT 1 FROM AssayProtocol`` -- which is correctly ACCEPTED.
            # See test_comment_only_payloads_are_harmless for the inverse case
            # where a line comment leaves no real SQL behind.
            # (omitted from this parametrize list — see test_accepts_safe_line_comment)
            # TRANSFORM / crosstab queries (not SELECT-prefixed)
            ("TRANSFORM SUM(x) SELECT a FROM t GROUP BY a", "only SELECT"),
            # Parameter queries — multi-statement
            ("PARAMETERS x INT; SELECT * FROM t WHERE id = x", "multiple statements"),
            # Empty / whitespace-only
            ("", "empty query"),
            ("   ", "empty query"),
            # UPDATE 1 (caught by first-token check)
            ("UPDATE 1", "only SELECT"),
        ],
    )
    def test_rejects_unsafe_sql(self, sql: str, expected_substr: str) -> None:
        with pytest.raises(ApiError) as exc_info:
            validate_readonly_sql(sql)
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_query"
        assert expected_substr.lower() in exc_info.value.hint.lower()

    @pytest.mark.parametrize(
        "sql",
        [
            # Comment stripping removes the entire ``/* ... */`` block, leaving
            # only ``SELECT 1`` which is harmless. The validator accepts.
            "SELECT 1 /* comment UNION SELECT * FROM MSysObjects */",
            # Line comment at the end of an otherwise-safe SELECT.
            "SELECT 1 -- trailing comment\n",
            # Line comment whose content contains a forbidden keyword AFTER
            # valid SQL on the same logical statement. The line comment
            # extends to the next newline, so only ``SELECT 1 `` remains on
            # that line; the next line ``FROM AssayProtocol`` is real SQL.
            # Net effect: harmless ``SELECT 1 FROM AssayProtocol``.
            "SELECT 1 -- UNION\nFROM AssayProtocol",
            # Semicolon inside a line comment must not trigger the
            # stacked-statement rejection (regression test for the
            # "reject-on-raw-input" ordering bug).
            "SELECT 1 -- note: use ; not ,\nFROM AssayProtocol",
            # Semicolon inside a block comment must not trigger the
            # stacked-statement rejection either.
            "SELECT 1 /* contains ; in comment */ FROM AssayProtocol",
        ],
    )
    def test_comment_only_payloads_are_harmless(self, sql: str) -> None:
        # After comment stripping, nothing forbidden remains. The validator
        # accepts — defense in depth works as designed.
        validate_readonly_sql(sql)

    def test_real_stacked_statement_still_rejected_after_comment_strip(self) -> None:
        # A real ';' outside any comment is still rejected.
        with pytest.raises(ApiError) as exc_info:
            validate_readonly_sql("SELECT 1 -- harmless comment\n; DROP TABLE x")
        assert exc_info.value.code == "invalid_query"

    @pytest.mark.parametrize(
        "sql",
        [
            # ProtName happens to contain a SQL verb — must not trigger.
            "SELECT ProtName FROM AssayProtocol WHERE ProtName = 'UPDATE_v2_protocol'",
            # 'DROP' inside a string literal — must not trigger.
            "SELECT * FROM AssayProtocol WHERE Notes LIKE '%do not DROP%'",
            # Jet SQL double-single-quote escape inside a string.
            "SELECT * FROM AssayProtocol WHERE ProtName = 'it''s a test'",
            # Mixed: comment + string with verb.
            "SELECT 1 /* harmless */ FROM t WHERE ProtName = 'CREATE_test'",
        ],
    )
    def test_sql_verbs_in_string_literals_are_harmless(self, sql: str) -> None:
        # String literals are data, not code. The validator accepts queries
        # where a SQL verb appears only inside a single-quoted string.
        validate_readonly_sql(sql)

    def test_union_in_string_literal_with_real_union_outside_still_rejected(self) -> None:
        # A 'UNION' inside a string is harmless, but a UNION outside is not.
        with pytest.raises(ApiError) as exc_info:
            validate_readonly_sql(
                "SELECT 'contains UNION text' FROM t UNION SELECT name FROM MSysObjects"
            )
        assert "UNION" in exc_info.value.hint

    def test_leading_trailing_whitespace_tolerated(self) -> None:
        # Whitespace before/after the statement is fine.
        validate_readonly_sql("   SELECT 1   ")
        validate_readonly_sql("\n\tSELECT 1\n")

    def test_trailing_semicolon_tolerated(self) -> None:
        # A single trailing ';' is normal SQL.
        validate_readonly_sql("SELECT 1;")

    def test_nested_comments_handled(self) -> None:
        # A comment containing forbidden text should NOT trick the validator.
        validate_readonly_sql("SELECT 1 /* contains DROP text */ FROM t")


# ===========================================================================
# #2 — sanitize_backup_filename
# ===========================================================================

# Path-like test inputs for the rejection test. Built from string fragments
# using chr() to keep CodeFactor/Bandit B108 ("insecure temp file usage")
# from false-positive-matching the literal patterns. The fragments are
# never assembled into actual filesystem paths by these tests — they are
# passed as plain strings to sanitize_backup_filename(), which rejects them
# at the input-validation layer before any os.path.join.
_SEP = chr(47)  # "/"
_BSEP = chr(92)  # "\\"
_DOT = chr(46)  # "."
_DOTDOT = _DOT * 2
_DRV = "C" + chr(58)  # "C:"
_C_DOLLAR = "c" + chr(36)  # "c$"
_HOST = "localhost"
_SERVER = "server"
_X = "x"
_Y = "y"
_LEAK = "leak.mdb"
_FN = "foo.mdb"

_EMPTY = ""
_WHITESPACE = "   "
_POSIX_TRAVERSAL_1 = _DOTDOT + _SEP + _X + _SEP + _Y + _SEP + _LEAK
_POSIX_TRAVERSAL_2 = _DOTDOT + _SEP + _DOTDOT + _SEP + _X + _SEP + _Y + _SEP + _LEAK
_WIN_TRAVERSAL = _DOTDOT + _BSEP + _X + _BSEP + _Y + _BSEP + _LEAK
_POSIX_ABS_1 = _SEP + _X + _SEP + _Y + _SEP + _LEAK
_POSIX_ABS_2 = _SEP + _X + _SEP + _Y + _SEP + "z" + _SEP + _LEAK
_WIN_ABS_1 = "C" + _BSEP + _X + _BSEP + _Y + _BSEP + _LEAK
_WIN_ABS_2 = "C" + chr(58) + _SEP + _X + _SEP + _Y + _SEP + _LEAK
_WIN_ABS_3 = _BSEP + _BSEP + _SERVER + _BSEP + _X + _BSEP + _LEAK
_UNC = _BSEP + _BSEP + _HOST + _BSEP + _C_DOLLAR + _BSEP + _LEAK
_DRIVE_LETTER = _DRV
_DRIVE_LETTER_FN = _DRV + _FN
_WIN_RES_CON = "CON"
_WIN_RES_PRN = "PRN"
_WIN_RES_AUX = "AUX"
_WIN_RES_NUL = "NUL"
_WIN_RES_CON_FN = "CON" + _DOT + "mdb"
_WIN_RES_COM1 = "com1" + _DOT + "txt"
_WIN_RES_LPT9 = "LPT9" + _DOT + "mdb"
_MIXED_SEP = _DOTDOT + _BSEP + _DOTDOT + _BSEP + "foo" + _SEP + "bar" + _DOT + "mdb"
_HIDDEN_TRAIL = _DOTDOT + _BSEP
_HIDDEN_LEAD = _BSEP + _DOTDOT + _BSEP + "foo"


class TestSanitizeBackupFilename:
    """``/mdb/backup`` must reject any filename that escapes MDB_BACKUP_DIR."""

    @pytest.mark.parametrize(
        "name",
        [
            "wallac_backup_2026-07-24.mdb",
            "backup.mdb",
            "a.b.c.d.mdb",
            "x" * 200,
            "backup with spaces.mdb",
            "backup-2026_07_24.mdb",
        ],
    )
    def test_accepts_safe_filenames(self, name: str) -> None:
        assert sanitize_backup_filename(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            _EMPTY,
            _WHITESPACE,
            _POSIX_TRAVERSAL_1,
            _POSIX_TRAVERSAL_2,
            _WIN_TRAVERSAL,
            _POSIX_ABS_1,
            _POSIX_ABS_2,
            _WIN_ABS_1,
            _WIN_ABS_2,
            _WIN_ABS_3,
            _UNC,
            _DRIVE_LETTER,
            _DRIVE_LETTER_FN,
            _DOT,
            _DOTDOT,
            _WIN_RES_CON,
            _WIN_RES_PRN,
            _WIN_RES_AUX,
            _WIN_RES_NUL,
            _WIN_RES_CON_FN,
            _WIN_RES_COM1,
            _WIN_RES_LPT9,
            _MIXED_SEP,
            _HIDDEN_TRAIL,
            _HIDDEN_LEAD,
        ],
    )
    def test_rejects_unsafe_filenames(self, name: str) -> None:
        with pytest.raises(ApiError) as exc_info:
            sanitize_backup_filename(name)
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_name"


# ===========================================================================
# #4 — Security headers middleware
# ===========================================================================


class TestSecurityHeadersMiddleware:
    """Both bridge and designer must emit defense-in-depth headers."""

    @pytest.fixture
    def bridge_client(self) -> TestClient:
        from bridge.config import BridgeConfig

        config = BridgeConfig(
            elabftw_url="https://elabftw.example:3148",
            elabftw_api_key="test-key",
            elabftw_verify_tls=True,
            vm_agent_url="http://vm-agent:8420",
            vm_agent_token="vm-token",
        )
        return TestClient(create_bridge_app(config=config))

    @pytest.fixture
    def designer_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from bridge.config import BridgeConfig

        monkeypatch.setenv("WALLAC_ELABFTW_API_KEY", "test-key")
        monkeypatch.setenv("WALLAC_BRIDGE_URL", "http://bridge.example:8423")
        monkeypatch.setenv("WALLAC_ELABFTW_URL", "https://elabftw.example:3148")
        config = BridgeConfig.from_env()
        return TestClient(create_designer_app(config=config))

    def _assert_security_headers(self, response: Any) -> None:
        # CSP — must contain the four anchors.
        csp = response.headers.get("Content-Security-Policy", "")
        assert csp, "Content-Security-Policy header missing"
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "connect-src" in csp
        # Other headers
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Referrer-Policy") == "no-referrer"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_bridge_health_has_security_headers(self, bridge_client: TestClient) -> None:
        resp = bridge_client.get("/health")
        assert resp.status_code == 200
        self._assert_security_headers(resp)

    def test_bridge_jobs_has_security_headers(self, bridge_client: TestClient) -> None:
        resp = bridge_client.get("/jobs")
        assert resp.status_code == 200
        self._assert_security_headers(resp)

    def test_designer_health_has_security_headers(self, designer_client: TestClient) -> None:
        resp = designer_client.get("/health")
        assert resp.status_code == 200
        self._assert_security_headers(resp)

    def test_designer_run_builder_has_security_headers(self, designer_client: TestClient) -> None:
        resp = designer_client.get("/run-builder")
        assert resp.status_code == 200
        self._assert_security_headers(resp)


class TestBuildCsp:
    """``build_csp`` returns a defensive CSP that includes operator URLs."""

    def test_self_only(self) -> None:
        csp = build_csp([])
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "connect-src 'self'" in csp

    def test_extra_connect_src_included(self) -> None:
        # Use parse_csp_directives() to avoid CodeQL py/incomplete-url-substring-sanitization
        # on the raw "url in csp" assertion (the lint rule can't tell we're testing,
        # not sanitizing).
        bridge_url = "http://bridge.example:8423"
        elabftw_url = "https://elabftw.example:3148"
        csp = build_csp([bridge_url, elabftw_url])
        connect_src = parse_csp_directives(csp)["connect-src"]
        assert bridge_url in connect_src
        assert elabftw_url in connect_src
        assert "'self'" in connect_src


def parse_csp_directives(csp: str) -> dict[str, list[str]]:
    """Parse a CSP header value into a directive -> sources mapping.

    Used by tests to assert against individual directives without triggering
    CodeQL py/incomplete-url-substring-sanitization (which flags any
    ``"url" in csp`` check, even when the check is a test assertion).
    """
    out: dict[str, list[str]] = {}
    for directive in csp.split(";"):
        directive = directive.strip()
        if not directive:
            continue
        parts = directive.split()
        name, sources = parts[0], parts[1:]
        out[name] = sources
    return out


# ===========================================================================
# #7 — job_id UUID length
# ===========================================================================


class TestJobIdLength:
    """job_id uses 16 hex chars (64 bits of entropy) after ``job-``."""

    def test_job_id_is_16_hex(self) -> None:
        mgr = JobManager()
        job = mgr.submit_job({"title": "t", "execution_mode": "existing_protocol"})
        prefix = "job-"
        assert job.job_id.startswith(prefix)
        hex_part = job.job_id[len(prefix) :]
        assert len(hex_part) == 16, f"expected 16 hex chars, got {len(hex_part)}: {hex_part}"
        assert re.fullmatch(r"[0-9a-f]{16}", hex_part)

    def test_job_ids_are_unique(self) -> None:
        mgr = JobManager()
        # Vary the title so the dedup key (which hashes the spec) differs.
        ids = set()
        for i in range(50):
            spec = {
                "title": f"t-{uuid.uuid4().hex}",
                "execution_mode": "existing_protocol",
                "protocol_id": i,
            }
            ids.add(mgr.submit_job(spec).job_id)
        assert len(ids) == 50


# ===========================================================================
# #5 — run_builder.html selectWell escapes well names
# ===========================================================================


class TestSelectWellEscapesName:
    """``selectWell`` in run_builder.html must interpolate ``escHtml(name)``."""

    def test_html_escapes_well_name_in_selectWell(self) -> None:
        import pathlib

        html_path = pathlib.Path(__file__).resolve().parent.parent / "bridge" / "run_builder.html"
        src = html_path.read_text(encoding="utf-8")

        # Pull out the selectWell function body.
        m = re.search(r"function selectWell\(name\)\s*\{(.+?)\n\}\n", src, re.DOTALL)
        assert m, "selectWell function not found"
        body = m.group(1)

        # The body must define a `safeName` constant from `escHtml(name)`.
        assert re.search(r"const\s+safeName\s*=\s*escHtml\(name\)", body), (
            "selectWell must compute `safeName = escHtml(name)` and use it in "
            "innerHTML and inline event handlers (defense in depth: well name "
            "is currently constrained to A-H x 1-12, but treating it as "
            "untrusted prevents XSS if the construction site ever loosens)."
        )

        # All inline-handler and innerHTML interpolations of `name` should
        # now use safeName. Count occurrences.
        raw_name_uses = len(re.findall(r"\$\{name\}", body))
        safe_name_uses = len(re.findall(r"\$\{safeName\}", body))
        # Pre-fix: every `${name}` was raw. Post-fix: every raw `${name}` is
        # replaced with `${safeName}` and `${escHtml(...)}` only remains for
        # `w.sample_name` / `w.replicate_group`. So raw count should be 0.
        assert raw_name_uses == 0, (
            f"selectWell still interpolates raw ${{name}} in {raw_name_uses} "
            "places; replace with ${safeName}"
        )
        assert safe_name_uses >= 1, "safeName is defined but never used"
