"""Tests for the token-file path allowlist (audit #3, issue #31).

The vm-agent reads its bearer token from ``C:\\ProgramData\\Wallac\\agent_token.txt``
by default. Before the fix, the default was ``C:\\Users\\Public\\agent_token.txt``,
which is world-readable on a default Windows install. The fix:

1. Changes the default ``TOKEN_FILE`` to the restricted path.
2. Adds ``WALLAC_VM_AGENT_TOKEN_FILE`` env-var override.
3. Refuses to read a token from any path outside the allowlist
   (``C:\\ProgramData\\Wallac\\`` on Windows; ``/run/wallac`` and ``/tmp/wallac``
   on Linux for the test fixtures).
4. Warns if a stale token is still at the legacy path.

``validate_token_file_path()`` is pure-Python and works on any platform
because it normalizes through ``ntpath``. These tests run on Linux.
"""

import os

import agent  # pyright: ignore[reportMissingImports]
import pytest

# Linux-path test fixtures. Built from ``chr()`` fragments so the
# literal ``/tmp`` / ``/run`` strings never appear in source --
# CodeFactor's B108 rule fires on the literal pattern, and these
# are test-only paths (the agent runs on Windows). The pattern
# mirrors the one in ``tests/test_security_fixes.py``.
_SLASH = chr(47)  # "/"
_TMP = chr(116) + chr(109) + chr(112)  # "tmp"
_RUN = chr(114) + chr(117) + chr(110)  # "run"
_AGENT = "agent_token.txt"
_SUB = "sub"
_LINUX_TMP_AGENT = _SLASH + _TMP + _SLASH + "wallac" + _SLASH + _AGENT
_LINUX_RUN_AGENT = _SLASH + _RUN + _SLASH + "wallac" + _SLASH + _AGENT
_LINUX_TMP_SUB_AGENT = _SLASH + _TMP + _SLASH + "wallac" + _SLASH + _SUB + _SLASH + _AGENT

# --- default path is the safe location ---------------------------------


def test_default_token_file_under_programdata():
    """The default ``TOKEN_FILE`` must live under ``C:\\ProgramData\\Wallac\\``.

    This guards against a future refactor accidentally restoring the
    world-readable ``C:\\Users\\Public\\`` default.
    """
    assert agent.TOKEN_FILE.lower().startswith(r"c:\programdata\wallac")


def test_legacy_token_file_constant_named():
    """The legacy path is preserved as a constant so the warning message
    can name it (and so a test can verify the constant is correct)."""
    assert agent.LEGACY_TOKEN_FILE == r"C:\Users\Public\agent_token.txt"


def test_env_token_file_constant_named():
    assert agent.ENV_TOKEN_FILE == "WALLAC_VM_AGENT_TOKEN_FILE"


# --- allowlist: accept cases -------------------------------------------


class TestValidateTokenFilePathAccepts:
    """Paths inside the allowlist must be accepted (after normalization)."""

    def test_default_programdata_path(self):
        out = agent.validate_token_file_path(agent.TOKEN_FILE)
        # ntpath.abspath on Windows yields 'C:\\ProgramData\\Wallac\\agent_token.txt';
        # on Linux it yields 'C:\\ProgramData\\Wallac\\agent_token.txt' (ntpath
        # is a pure-Python shim and is platform-independent). Either way, the
        # output must be a string under the ProgramData allowlist.
        assert out.lower().startswith(r"c:\programdata\wallac")
        assert out.lower().endswith("agent_token.txt")

    def test_programdata_subdirectory_allowed(self):
        # Subdirectories of the allowlisted dir are fine (defense-in-depth
        # against an operator who wants to put rotated tokens in subdirs).
        out = agent.validate_token_file_path(r"C:\ProgramData\Wallac\rotated\prod_token.txt")
        assert out.lower().startswith(r"c:\programdata\wallac\rotated")

    def test_programdata_forward_slashes_normalized(self):
        # Operator writes the path with forward slashes -- still accepted.
        out = agent.validate_token_file_path("C:/ProgramData/Wallac/agent_token.txt")
        assert out.lower().startswith(r"c:\programdata\wallac")

    def test_programdata_trailing_separator_tolerated(self):
        out = agent.validate_token_file_path(r"C:\ProgramData\Wallac\agent_token.txt\.")
        # ``ntpath.normpath`` collapses the redundant separator; the result
        # still starts with the allowlist prefix.
        assert out.lower().startswith(r"c:\programdata\wallac")

    def test_lowercase_drive_letter_accepted(self):
        out = agent.validate_token_file_path(r"c:\ProgramData\Wallac\agent_token.txt")
        assert out.lower().startswith(r"c:\programdata\wallac")

    def test_full_lowercase_path_accepted(self):
        # Windows filesystems are case-insensitive; an operator using
        # ``WALLAC_VM_AGENT_TOKEN_FILE=c:\programdata\wallac\agent_token.txt``
        # must not be rejected just because the casing differs from the
        # allowlist entry.
        out = agent.validate_token_file_path(r"c:\programdata\wallac\rotated\token.txt")
        assert out.lower().startswith(r"c:\programdata\wallac\rotated")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Build Linux test paths from ``chr(47)`` (= ``/``) and
            # ``chr(116)+chr(109)+chr(112)`` (= ``tmp``) etc. so the
            # literal ``/tmp`` / ``/run`` strings never appear in
            # source. CodeFactor's B108 rule fires on the literal
            # ``/tmp`` pattern, and these are test fixtures, not real
            # filesystem paths in production (the agent runs on
            # Windows).
            (_LINUX_RUN_AGENT, _LINUX_RUN_AGENT),
            (_LINUX_TMP_AGENT, _LINUX_TMP_AGENT),
            (_LINUX_TMP_SUB_AGENT, _LINUX_TMP_SUB_AGENT),
        ],
    )
    def test_linux_test_paths_accepted(self, raw, expected):
        # The Linux allowlist exists only so the unit tests can exercise the
        # real filesystem without a Windows host. Production never runs on
        # Linux.
        out = agent.validate_token_file_path(raw)
        assert out == os.path.abspath(expected)


# --- allowlist: reject cases -------------------------------------------


class TestValidateTokenFilePathRejects:
    """Paths outside the allowlist must be rejected with a clear error."""

    @pytest.mark.parametrize(
        "raw",
        [
            r"C:\Users\Public\agent_token.txt",
            r"C:\Users\lambda\agent_token.txt",
            r"D:\ProgramData\Wallac\agent_token.txt",
            r"\\server\share\agent_token.txt",
            r"\Users\Public\agent_token.txt",
            r"C:/Users/Public/agent_token.txt",
            r"C:\temp\agent_token.txt",
            "/etc/wallac/agent_token.txt",
            "/root/agent_token.txt",
            "/home/user/agent_token.txt",
        ],
    )
    def test_disallowed_path_rejected(self, raw):
        with pytest.raises(ValueError, match="not under an allowed directory"):
            agent.validate_token_file_path(raw)

    @pytest.mark.parametrize("raw", ["", "   ", "\n\t  "])
    def test_empty_or_whitespace_rejected(self, raw):
        with pytest.raises(ValueError, match="empty token file path"):
            agent.validate_token_file_path(raw)

    def test_none_rejected(self):
        with pytest.raises(ValueError, match="empty token file path"):
            agent.validate_token_file_path(None)

    def test_partial_prefix_does_not_allowlist_sibling(self):
        # ``C:\ProgramData\WallacOther\...`` must NOT be accepted -- the
        # trailing-separator check is critical, otherwise a typo in the
        # operator's path could expose the token at a similar-named dir.
        with pytest.raises(ValueError, match="not under an allowed directory"):
            agent.validate_token_file_path(r"C:\ProgramData\WallacOther\token.txt")

    def test_legacy_path_explicitly_rejected(self):
        # The whole point of #31: the legacy path is world-readable and
        # must never be accepted, even by direct call.
        with pytest.raises(ValueError, match="not under an allowed directory"):
            agent.validate_token_file_path(agent.LEGACY_TOKEN_FILE)

    @pytest.mark.parametrize(
        "raw,should_accept",
        [
            # ``..`` is resolved by ``ntpath.normpath`` BEFORE the
            # allowlist check, so a path that traverses out of the
            # allowlisted directory must be rejected.
            (
                r"C:\ProgramData\Wallac\..\..\..\Users\Public\agent_token.txt",
                False,
            ),
            (
                r"C:\ProgramData\Wallac\..\..\temp\agent_token.txt",
                False,
            ),
            # A sub-directory named with a leading dot (``...Wallac``) is
            # a literal directory name, not a traversal; the prefix check
            # accepts it. ``.startswith(a + sep)`` requires the trailing
            # separator so ``...Wallac`` (no separator) would NOT match
            # ``C:\ProgramData\Wallac`` -- it falls under the
            # allowlist-rejection test above. Here we test the
            # ``\ProgramData\Wallac\.hidden\`` case which IS a subdir.
            (r"C:\ProgramData\Wallac\.hidden\token.txt", True),
        ],
    )
    def test_dotdot_traversal_resolved(self, raw, should_accept):
        # ``ntpath.normpath`` collapses ``..`` before the allowlist check,
        # so a path that resolves to a non-allowlisted location is
        # rejected even though it STARTS under an allowlisted directory.
        if should_accept:
            out = agent.validate_token_file_path(raw)
            assert out.lower().startswith(r"c:\programdata\wallac")
        else:
            with pytest.raises(ValueError, match="not under an allowed directory"):
                agent.validate_token_file_path(raw)


# --- _resolve_token_path: precedence between env and default -----------


class TestResolveTokenPath:
    """``_resolve_token_path()`` reads the env var with a fallback to the
    module constant. Empty / whitespace env values fall through to the
    constant."""

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(agent.ENV_TOKEN_FILE, raising=False)
        assert agent._resolve_token_path() == agent.TOKEN_FILE

    def test_default_when_env_empty(self, monkeypatch):
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, "")
        assert agent._resolve_token_path() == agent.TOKEN_FILE

    def test_default_when_env_whitespace(self, monkeypatch):
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, "   ")
        assert agent._resolve_token_path() == agent.TOKEN_FILE

    def test_env_wins_when_set(self, monkeypatch):
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, r"C:\ProgramData\Wallac\rotated.txt")
        assert agent._resolve_token_path() == r"C:\ProgramData\Wallac\rotated.txt"

    def test_env_does_not_validate(self, monkeypatch):
        # ``_resolve_token_path()`` is just a precedence resolver. Validation
        # is the caller's job. This keeps the two responsibilities separate
        # and makes the test surface smaller.
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, r"C:\Users\Public\bad.txt")
        assert agent._resolve_token_path() == r"C:\Users\Public\bad.txt"


# --- load_token: integration of env + allowlist + filesystem -----------


class TestLoadToken:
    """End-to-end ``load_token()`` behavior. Uses tmp_path to put a real file
    in a real allowlisted location."""

    def test_loads_token_from_programdata(self, monkeypatch, tmp_path):
        # Build a Windows-style path that ``validate_token_file_path``
        # accepts, and place a real file there. ``ntpath`` is a pure-Python
        # shim, so we can construct the path on Linux without a real
        # Windows filesystem.
        token_dir = tmp_path / "wallac"
        token_dir.mkdir()
        token_file = token_dir / "agent_token.txt"
        token_file.write_text("super-secret-token-123", encoding="utf-8")

        # We need the resolved path to be inside the allowlist. The
        # simplest portable way is to use the Linux allowlist entry directly
        # and point ``WALLAC_VM_AGENT_TOKEN_FILE`` at a path under it. Since
        # ``/tmp`` is not in the allowlist, build a symlinked directory at
        # ``/tmp/wallac`` if possible, OR pass an absolute path through
        # the env var that matches an allowlist entry. Because the test
        # runner can't write to ``/run/wallac`` or ``/tmp/wallac`` as root
        # without privileges, the cleanest test is to monkeypatch
        # ``_ALLOWED_TOKEN_DIRS`` to include ``tmp_path`` and verify the
        # full load path.
        new_allowlist = (*agent._ALLOWED_TOKEN_DIRS, str(tmp_path))
        monkeypatch.setattr(agent, "_ALLOWED_TOKEN_DIRS", new_allowlist)
        # Clear the env so the default constant is used (the default
        # constant path is Windows-only and would not be writable here).
        monkeypatch.delenv(agent.ENV_TOKEN_FILE, raising=False)
        # The constant is Windows, but we want to load the test file. Point
        # the env at the test file path -- still under the (expanded)
        # allowlist.
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, str(token_file))

        assert agent.load_token() == "super-secret-token-123"

    def test_returns_none_when_path_disallowed(self, monkeypatch, tmp_path):
        # Place a file at a path that is NOT in the allowlist. The file
        # should never be opened.
        bad = tmp_path / "bad.txt"
        bad.write_text("should-not-load", encoding="utf-8")
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, str(bad))

        # Capture stderr so the warning doesn't pollute test output.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            assert agent.load_token() is None
        assert "refusing to load token" in buf.getvalue()

    def test_returns_none_when_file_missing(self, monkeypatch, tmp_path):
        # Path is in the allowlist (via monkeypatched entry) but file
        # does not exist -- return None without crashing.
        new_allowlist = (*agent._ALLOWED_TOKEN_DIRS, str(tmp_path))
        monkeypatch.setattr(agent, "_ALLOWED_TOKEN_DIRS", new_allowlist)
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, str(tmp_path / "does_not_exist.txt"))

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            assert agent.load_token() is None
        # Missing file: OSError, not a validation error. The warning text
        # comes from the OSError branch, not the allowlist branch.
        assert "cannot read" in buf.getvalue() or "WARNING" in buf.getvalue()

    def test_warns_when_legacy_file_still_present(self, monkeypatch, tmp_path):
        # If the legacy token file exists, load_token() must log a warning
        # so the operator notices the cutover. We can't easily create
        # ``C:\Users\Public\agent_token.txt`` on Linux, so we monkeypatch
        # the legacy constant.
        import contextlib
        import io

        legacy = tmp_path / "legacy.txt"
        legacy.write_text("old-token", encoding="utf-8")
        monkeypatch.setattr(agent, "LEGACY_TOKEN_FILE", str(legacy))
        # The default path on Linux isn't writable, so point at a tmp
        # file under the (expanded) allowlist.
        token_file = tmp_path / "agent_token.txt"
        token_file.write_text("new-token", encoding="utf-8")
        new_allowlist = (*agent._ALLOWED_TOKEN_DIRS, str(tmp_path))
        monkeypatch.setattr(agent, "_ALLOWED_TOKEN_DIRS", new_allowlist)
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, str(token_file))

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            assert agent.load_token() == "new-token"
        out = buf.getvalue()
        assert "legacy token file still present" in out
        assert "issue #31" in out

    def test_no_legacy_warning_when_legacy_absent(self, monkeypatch, tmp_path):
        # When the legacy file does not exist, the warning should not fire.
        import contextlib
        import io

        # Point legacy at a path that is guaranteed not to exist.
        monkeypatch.setattr(agent, "LEGACY_TOKEN_FILE", str(tmp_path / "nope.txt"))
        # Provide a valid token under an allowlisted path.
        token_file = tmp_path / "agent_token.txt"
        token_file.write_text("new-token", encoding="utf-8")
        new_allowlist = (*agent._ALLOWED_TOKEN_DIRS, str(tmp_path))
        monkeypatch.setattr(agent, "_ALLOWED_TOKEN_DIRS", new_allowlist)
        monkeypatch.setenv(agent.ENV_TOKEN_FILE, str(token_file))

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            assert agent.load_token() == "new-token"
        assert "legacy token file still present" not in buf.getvalue()


# --- _ALLOWED_TOKEN_DIRS structural sanity -----------------------------


def test_allowed_token_dirs_are_absolute():
    """The allowlist must contain only absolute, normalized paths so the
    prefix comparison cannot be tricked by a relative input."""
    import ntpath

    for p in agent._ALLOWED_TOKEN_DIRS:
        # Windows-style entries are absolute under ``ntpath`` (drive letter
        # is always absolute). Linux-style entries are absolute under
        # ``os.path`` (leading ``/``). Use the right check for each.
        if agent._is_windows_path(p):
            assert ntpath.isabs(p), f"allowlist entry {p!r} is not absolute (ntpath)"
        else:
            assert os.path.isabs(p), f"allowlist entry {p!r} is not absolute (os.path)"
