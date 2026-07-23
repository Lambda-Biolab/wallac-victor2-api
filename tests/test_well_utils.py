"""Tests for bridge.well_utils (extracted from execution to break a cycle)."""

from __future__ import annotations

from bridge.well_utils import normalize_well_name, well_key


class TestNormalizeWellName:
    def test_zero_padded_to_canonical(self):
        """A01 -> A1, H12 -> H12."""
        assert normalize_well_name("A01") == "A1"
        assert normalize_well_name("H12") == "H12"

    def test_already_canonical_unchanged(self):
        assert normalize_well_name("A1") == "A1"
        assert normalize_well_name("D6") == "D6"

    def test_lowercase_to_uppercase(self):
        assert normalize_well_name("a1") == "A1"

    def test_with_whitespace_stripped(self):
        assert normalize_well_name("  A01  ") == "A1"

    def test_empty_returns_empty(self):
        assert normalize_well_name("") == ""

    def test_unknown_passthrough(self):
        """Names that don't match the pattern are returned unchanged."""
        assert normalize_well_name("P1") == "P1"
        assert normalize_well_name("Z99") == "Z99"

    def test_strips_leading_zeros(self):
        """A01 -> A1 (int("01") == 1)."""
        assert normalize_well_name("A01") == "A1"
        assert normalize_well_name("A001") == "A001"  # not matched by 2-digit pattern


class TestWellKey:
    def test_prefers_well_name(self):
        assert well_key({"well_name": "A1", "well": "A01"}) == "A1"

    def test_falls_back_to_well(self):
        assert well_key({"well": "A01"}) == "A1"

    def test_empty_when_neither(self):
        assert well_key({}) == ""

    def test_handles_none_values(self):
        assert well_key({"well_name": None, "well": "A01"}) == "A1"
