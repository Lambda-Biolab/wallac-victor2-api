"""Tests for protocol-name whitespace normalization.

This is a regression guard for the
``docs/plans/wallac-existing-protocol-writeback-repair.md`` Slice 1 fix.

Two resolvers must agree on whitespace-collapsed names:

* ``vm-agent/agent.py::_resolve_protocol`` — matches against the MDB's
  installed protocols.
* ``bridge/executor.py::BridgeExecutor._find_protocol_by_name`` — the
  fallback path the bridge uses when the vm-agent direct lookup returns
  404.

The reported failure was the eLabFTW Method record carrying a name with
a stray space (``Absorbance @ 610 (1.0 s)``) while the canonical MDB
name has no space (``Absorbance @ 610 (1.0s)``). The id path
(``protocol_id=2000008``) always worked; only the name path broke.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from typing import Any

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_VM_AGENT = _REPO_ROOT / "vm-agent"


@pytest.fixture(scope="module")
def vm_agent_module() -> types.ModuleType:
    """Import ``vm-agent/agent.py`` so we can drive its pure helpers.

    The module pulls in Windows-only modules at top-level (ctypes.windll,
    comtypes). ``tests/test_vm_agent_imports.py`` already established the
    pattern of stubbing those out for Linux unit tests. We follow the same
    stub strategy here, which lets us call ``_resolve_protocol`` and
    ``_normalize_protocol_name`` without ever invoking any Win32 API.
    """
    sys.modules.setdefault("ctypes", __import__("ctypes"))
    comtypes_stub = types.ModuleType("comtypes")

    class _Dummy:
        def __getattr__(self, _name: str) -> _Dummy:
            return self

        def __call__(self, *args: Any, **kwargs: Any) -> _Dummy:
            return self

    comtypes_stub.client = _Dummy()
    comtypes_stub.GUID = _Dummy
    sys.modules.setdefault("comtypes", comtypes_stub)
    spec = importlib.util.spec_from_file_location("vm_agent_under_test", _VM_AGENT / "agent.py")
    assert spec and spec.loader, "could not load vm-agent/agent.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubWorker:
    """Stand-in for ``ComWorker`` — just enough surface for the resolver."""

    def __init__(self, protocols: list[dict[str, Any]]) -> None:
        self._protocols = protocols
        self.refresh_calls = 0

    def call(self, fn, timeout: float = 40) -> Any:
        # op_protocols(False) returns the cached list; op_protocols(True)
        # refreshes from the MDB. We expose both as the same list — the
        # resolver only checks identity-by-id on a refresh miss.
        self.refresh_calls += 1
        return self._protocols


class TestNormalizeProtocolName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Absorbance @ 610 (1.0s)", "Absorbance @ 610 (1.0s)"),
            ("Absorbance @ 610 (1.0 s)", "Absorbance @ 610 (1.0s)"),
            ("  Absorbance @  610 (  1.0  s  )  ", "Absorbance @ 610 (1.0s)"),
            ("Absorbance @ 610 (1.0ms)", "Absorbance @ 610 (1.0ms)"),
            ("Absorbance @ 610 (1.0 ms)", "Absorbance @ 610 (1.0ms)"),
        ],
    )
    def test_collapses_whitespace(self, vm_agent_module, raw: str, expected: str) -> None:
        assert vm_agent_module._normalize_protocol_name(raw) == expected


class TestVmAgentResolveProtocol:
    def _fixtures(self) -> list[dict[str, Any]]:
        return [
            {"id": 2000008, "name": "Absorbance @ 610 (1.0s)"},
            {"id": 2000009, "name": "Absorbance @ 610 (0.1s)"},
            {"id": 2000000, "name": "Absorbance @ 600 (0.1s)"},
        ]

    def test_exact_match_returns_protocol(self, vm_agent_module) -> None:
        worker = _StubWorker(self._fixtures())
        result = vm_agent_module._resolve_protocol("Absorbance @ 610 (1.0s)", worker)
        assert result["id"] == 2000008

    def test_space_before_unit_suffix_resolves(self, vm_agent_module) -> None:
        """Reported regression: ``"Absorbance @ 610 (1.0 s)"`` (with space)
        must resolve to id 2000008 (``"Absorbance @ 610 (1.0s)"``)."""
        worker = _StubWorker(self._fixtures())
        result = vm_agent_module._resolve_protocol("Absorbance @ 610 (1.0 s)", worker)
        assert result["id"] == 2000008
        assert result["name"] == "Absorbance @ 610 (1.0s)"

    def test_id_path_is_unaffected(self, vm_agent_module) -> None:
        worker = _StubWorker(self._fixtures())
        result = vm_agent_module._resolve_protocol("2000008", worker)
        assert result["id"] == 2000008

    def test_case_insensitive_after_normalization(self, vm_agent_module) -> None:
        worker = _StubWorker(self._fixtures())
        result = vm_agent_module._resolve_protocol("ABSORBANCE @ 610 (1.0 s)", worker)
        assert result["id"] == 2000008

    def test_substring_match_still_works(self, vm_agent_module) -> None:
        worker = _StubWorker([{"id": 1, "name": "Fluorometry @ 535 (0.1s)"}])
        result = vm_agent_module._resolve_protocol("Fluorometry", worker)
        assert result["id"] == 1

    def test_ambiguous_substring_still_raises_409(self, vm_agent_module) -> None:
        """Whitespace normalization must NOT collapse a legitimate
        substring ambiguity into a single match. The query
        ``"Absorbance @ 610"`` matches both ``Absorbance @ 610 (1.0s)``
        and ``Absorbance @ 610 (0.1s)`` after normalization and the
        resolver must still return 409."""
        worker = _StubWorker(
            [
                {"id": 1, "name": "Absorbance @ 610 (1.0s)"},
                {"id": 2, "name": "Absorbance @ 610 (0.1s)"},
            ]
        )
        with pytest.raises(vm_agent_module.ApiError) as exc:
            vm_agent_module._resolve_protocol("Absorbance @ 610", worker)
        assert exc.value.status == 409

    def test_normalization_does_not_shadow_distinct_protocols(self, vm_agent_module) -> None:
        """A query that EXACTLY normalizes to one protocol must not
        accidentally pick a different one. The query
        ``"Absorbance @ 610 (1.0 s)"`` normalizes to the canonical
        ``"absorbance @ 610 (1.0s)"`` form — it must resolve to id 1
        (the 1.0s preset) and not id 2 (the 0.1s preset)."""
        worker = _StubWorker(
            [
                {"id": 1, "name": "Absorbance @ 610 (1.0s)"},
                {"id": 2, "name": "Absorbance @ 610 (0.1s)"},
            ]
        )
        result = vm_agent_module._resolve_protocol("Absorbance @ 610 (1.0 s)", worker)
        assert result["id"] == 1

    def test_unknown_name_raises_404(self, vm_agent_module) -> None:
        worker = _StubWorker(self._fixtures())
        with pytest.raises(vm_agent_module.ApiError) as exc:
            vm_agent_module._resolve_protocol("Luminometry @ 999 (1.0s)", worker)
        assert exc.value.status == 404


class TestBridgeExecutorFindProtocolByName:
    """The bridge-side fallback must also handle whitespace differences.

    The bridge calls ``vm_agent.get_protocol(name)`` first; if that returns
    404 (older vm-agent builds without normalization), the bridge falls
    back to ``_find_protocol_by_name`` over the protocol listing. The
    fallback must normalize too, otherwise eLabFTW-supplied names fail in
    both layers.
    """

    def _make_executor(self):
        from bridge.executor import BridgeExecutor

        # Bypass config — these tests only exercise _find_protocol_by_name
        # which only touches the vm_agent attribute. Pass ``None`` clients
        # since we override ``get_protocols`` per-test.
        executor = BridgeExecutor(vm_agent=None, elabftw=None, dry_run=True)  # type: ignore[arg-type]

        class _ListingVmAgent:
            def __init__(self, protocols: list[dict[str, Any]]) -> None:
                self._protocols = protocols

            def get_protocols(self, refresh: bool = False) -> list[dict[str, Any]]:
                return self._protocols

        return executor, _ListingVmAgent

    def test_exact_match_wins_over_normalized_match(self) -> None:
        executor, VmAgentCls = self._make_executor()
        executor.vm_agent = VmAgentCls(
            [
                {"id": 1, "name": "Absorbance @ 610 (1.0 s)"},  # exact
                {"id": 2, "name": "Absorbance @ 610 (1.0s)"},  # normalized
            ]
        )
        result = executor._find_protocol_by_name("Absorbance @ 610 (1.0 s)")
        assert result is not None
        assert result["id"] == 1

    def test_normalized_match_used_when_exact_absent(self) -> None:
        executor, VmAgentCls = self._make_executor()
        executor.vm_agent = VmAgentCls(
            [
                {"id": 2000008, "name": "Absorbance @ 610 (1.0s)"},
                {"id": 2000009, "name": "Absorbance @ 610 (0.1s)"},
            ]
        )
        result = executor._find_protocol_by_name("Absorbance @ 610 (1.0 s)")
        assert result is not None
        assert result["id"] == 2000008

    def test_returns_none_when_no_match(self) -> None:
        executor, VmAgentCls = self._make_executor()
        executor.vm_agent = VmAgentCls(
            [
                {"id": 2000008, "name": "Absorbance @ 610 (1.0s)"},
            ]
        )
        result = executor._find_protocol_by_name("Luminometry @ 999 (1.0s)")
        assert result is None

    def test_id_match_not_attempted_by_fallback(self) -> None:
        """The bridge fallback only matches names — ID resolution is
        handled in ``_resolve_existing_protocol`` before this is called."""
        executor, VmAgentCls = self._make_executor()
        executor.vm_agent = VmAgentCls(
            [
                {"id": 2000008, "name": "Absorbance @ 610 (1.0s)"},
            ]
        )
        result = executor._find_protocol_by_name("2000008")
        assert result is None

    def test_ambiguous_normalized_match_returns_none(self) -> None:
        """Review-blocker 1: the bridge fallback must NOT silently pick
        the first of two protocols that happen to normalize identically.
        Two installed factory presets ``Absorbance @ 600 (1.0s)`` and
        ``Absorbance @ 600 (1.0 s)`` normalize to the same string. A
        caller passing a third variant (e.g. ``Absorbance @ 600(1.0s)``
        — no space after ``@`` would be unusual, but anything that
        normalizes to the same string) must NOT pick whichever protocol
        the listing happens to return first. Returning ``None`` makes
        the caller fail the job with a 404, matching the vm-agent's
        409 ambiguity behavior."""
        executor, VmAgentCls = self._make_executor()
        executor.vm_agent = VmAgentCls(
            [
                {"id": 1, "name": "Absorbance @ 600 (1.0s)"},
                {"id": 2, "name": "Absorbance @ 600 (1.0 s)"},
            ]
        )
        result = executor._find_protocol_by_name("Absorbance @ 600 (1.0 s)")
        # Both raw names appear in the listing, so this is an exact
        # match — the first one wins. The interesting case is when
        # the query does NOT exactly match either raw name.
        assert result is not None  # exact match wins for raw query

        executor.vm_agent = VmAgentCls(
            [
                {"id": 1, "name": "Absorbance @ 600 (1.0s)"},
                {"id": 2, "name": "Absorbance @ 600 (1.0 s)"},
            ]
        )
        # Query that does not exactly match either, but normalizes to
        # the same string. Bridge must refuse (None) rather than pick
        # the first listing entry.
        result = executor._find_protocol_by_name("absorbance @ 600 (1.0 s)")
        assert result is None


class TestVmAgentWellsDefensive:
    """The vm-agent's ``_wells_to_plate_map`` must reject legacy wrapped
    shapes that the previous bridge build leaked through. This is the
    fail-closed seam for slice 2 of the writeback repair plan — even
    if a future buggy bridge build regresses, the vm-agent must not
    silently produce an empty plate map."""

    def test_top_level_all_sets_full_grid(self, vm_agent_module) -> None:
        body = {"all": True}
        plate_map = vm_agent_module._wells_to_plate_map(body)
        # 108 bytes total: 12-byte header + 96-byte grid all set.
        assert len(plate_map) == 108
        assert plate_map[12:] == [1] * 96

    def test_top_level_rows_sets_named_rows(self, vm_agent_module) -> None:
        plate_map = vm_agent_module._wells_to_plate_map({"rows": ["A", "B"]})
        # Row A is grid index 0..11, row B is 12..23.
        grid = plate_map[12:]
        assert grid[0:12] == [1] * 12
        assert grid[12:24] == [1] * 12
        assert grid[24:96] == [0] * 72

    def test_top_level_wells_sets_explicit_wells(self, vm_agent_module) -> None:
        plate_map = vm_agent_module._wells_to_plate_map({"wells": ["A1", "B12"]})
        grid = plate_map[12:]
        assert grid[0] == 1  # A1
        assert grid[23] == 1  # B12
        assert sum(grid) == 2

    def test_wrapped_wells_spec_envelope_rejected(self, vm_agent_module) -> None:
        """The exact shape that caused the production failure:
        ``{"wells_spec": {"all": True}}``. The legacy vm-agent returned
        a confusing 400; the new vm-agent returns a clear one that
        names the offender."""
        with pytest.raises(vm_agent_module.ApiError) as exc:
            vm_agent_module._wells_to_plate_map({"wells_spec": {"all": True}})
        assert exc.value.status == 400
        assert "wells_spec" in str(exc.value.hint) or "wells_spec" in str(exc.value.detail)

    def test_wells_spec_envelope_with_real_keys_uses_real_keys(self, vm_agent_module) -> None:
        """If the envelope is present *and* the real keys are also at
        the top level, the real keys win (defensive but not
        over-zealous)."""
        plate_map = vm_agent_module._wells_to_plate_map({"wells_spec": {"all": True}, "all": True})
        assert len(plate_map) == 108
        assert plate_map[12:] == [1] * 96

    def test_non_dict_body_rejected(self, vm_agent_module) -> None:
        with pytest.raises(vm_agent_module.ApiError) as exc:
            vm_agent_module._wells_to_plate_map([])  # type: ignore[arg-type]
        assert exc.value.status == 400
