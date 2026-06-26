"""Tests for canonical JSON serialization, SHA-256 hashing, and v1 schema
validation for the Wallac Victor2 protocol authoring pipeline.

Covers: golden fixture stability, hash determinism, fail-closed hash
verification, schema version gating, round-trips, well-name validation,
mode validation, execution-mode validation, and canonical JSON properties.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from bridge.canonical import canonicalize, compute_hash, verify_hash
from bridge.errors import CANONICAL_HASH_MISMATCH, SCHEMA_UNSUPPORTED, BridgeError
from bridge.schemas import (
    VALID_WELL_NAMES,
    AnalysisSpec,
    JobSpec,
    LayoutSpec,
    MethodSpec,
    is_valid_well_name,
    validate_schema_identity,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "golden"

GOLDEN_FILES: dict[str, pathlib.Path] = {
    "method_photometry": GOLDEN_DIR / "method_photometry.json",
    "method_fluorometry": GOLDEN_DIR / "method_fluorometry.json",
    "method_luminescence": GOLDEN_DIR / "method_luminescence.json",
    "layout_96well": GOLDEN_DIR / "layout_96well.json",
    "analysis_plan": GOLDEN_DIR / "analysis_plan.json",
    "job_generated_reusable": GOLDEN_DIR / "job_generated_reusable.json",
    "job_generated_oneoff": GOLDEN_DIR / "job_generated_oneoff.json",
    "job_existing": GOLDEN_DIR / "job_existing.json",
}

# Map each golden file to the appropriate from_dict classmethod.
GOLDEN_SCHEMA_CLASSES: dict[str, type] = {
    "method_photometry": MethodSpec,
    "method_fluorometry": MethodSpec,
    "method_luminescence": MethodSpec,
    "layout_96well": LayoutSpec,
    "analysis_plan": AnalysisSpec,
    "job_generated_reusable": JobSpec,
    "job_generated_oneoff": JobSpec,
    "job_existing": JobSpec,
}


# ---------------------------------------------------------------------------
# 1. Golden fixture stability
# ---------------------------------------------------------------------------

GOLDEN_NAMES = list(GOLDEN_FILES.keys())


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_bytes_match_canonicalized_json(name):
    """Reading golden bytes and re-canonicalizing the parsed dict must
    produce byte-for-byte identical output."""
    path = GOLDEN_FILES[name]
    golden_bytes = path.read_bytes()
    data = json.loads(golden_bytes)
    canonicalized = canonicalize(data)
    assert canonicalized == golden_bytes, f"{name}: re-canonicalized bytes differ from golden file"


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_hash_is_64_char_lowercase_hex(name):
    """SHA-256 hash of the golden file bytes must be a 64-character lowercase
    hex string."""
    path = GOLDEN_FILES[name]
    golden_bytes = path.read_bytes()
    h = compute_hash(golden_bytes)
    assert len(h) == 64, f"{name}: expected 64-char hex hash, got {len(h)}"
    assert h == h.lower(), f"{name}: hash must be lowercase"
    assert all(c in "0123456789abcdef" for c in h), f"{name}: hash contains non-hex chars"


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_from_dict_to_dict_roundtrip_matches_golden(name):
    """Parsing with from_dict, then to_dict + canonicalize must match golden."""
    path = GOLDEN_FILES[name]
    golden_bytes = path.read_bytes()
    data = json.loads(golden_bytes)
    klass = GOLDEN_SCHEMA_CLASSES[name]
    spec = klass.from_dict(data)
    re_canonicalized = canonicalize(spec.to_dict())
    assert re_canonicalized == golden_bytes, (
        f"{name}: from_dict → to_dict → canonicalize differs from golden"
    )


# ---------------------------------------------------------------------------
# 2. Hash determinism
# ---------------------------------------------------------------------------


def test_same_dict_produces_same_hash():
    """Canonicalizing the same dict twice produces identical bytes and hash."""
    data = {"schema_name": "wallac.method", "schema_version": 1, "mode": "photometry"}
    b1 = canonicalize(data)
    b2 = canonicalize(data)
    assert b1 == b2
    assert compute_hash(b1) == compute_hash(b2)


def test_different_dicts_produce_different_hashes():
    """Two dicts with different content must produce different SHA-256 hashes."""
    a = {"schema_name": "wallac.method", "schema_version": 1, "mode": "photometry"}
    b = {"schema_name": "wallac.method", "schema_version": 1, "mode": "fluorometry"}
    assert compute_hash(canonicalize(a)) != compute_hash(canonicalize(b))


def test_hash_is_always_64_char_lowercase_hex():
    """Any hash produced by compute_hash must pass the format invariant."""
    samples = [
        {"a": 1},
        {"schema_name": "wallac.job"},
        {"nested": {"key": "val", "num": 42}},
    ]
    for s in samples:
        h = compute_hash(canonicalize(s))
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# 3. Hash mismatch fails closed
# ---------------------------------------------------------------------------

_ANY_DATA = b'{"a":1}'


def test_verify_hash_wrong_hash_raises():
    """verify_hash with a wrong expected hash raises BridgeError(CANONICAL_HASH_MISMATCH)."""
    actual_hash = compute_hash(_ANY_DATA)
    # Flip the first hex digit to guarantee mismatch.
    bad_hash = ("f" if actual_hash[0] != "f" else "0") + actual_hash[1:]
    with pytest.raises(BridgeError) as exc_info:
        verify_hash(_ANY_DATA, bad_hash)
    assert exc_info.value.code == CANONICAL_HASH_MISMATCH
    assert "Canonical hash mismatch" in exc_info.value.human_message


def test_verify_hash_correct_hash_does_not_raise():
    """verify_hash with the exact correct hash does not raise."""
    actual_hash = compute_hash(_ANY_DATA)
    verify_hash(_ANY_DATA, actual_hash)  # must not raise


def test_verify_hash_case_insensitive():
    """verify_hash accepts an uppercase expected_hash by lowercasing it."""
    actual_hash = compute_hash(_ANY_DATA)
    verify_hash(_ANY_DATA, actual_hash.upper())  # must not raise


# ---------------------------------------------------------------------------
# 4. Schema unsupported / future version fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema_name,schema_version",
    [
        ("wallac.method", 2),
        ("wallac.unknown", 1),
        ("wallac.layout", 99),
        ("wallac.analysis", 0),
        ("wallac.job", 3),
    ],
)
def test_validate_schema_identity_unsupported(schema_name, schema_version):
    """Unknown name or unsupported version raises BridgeError(SCHEMA_UNSUPPORTED)."""
    with pytest.raises(BridgeError) as exc_info:
        validate_schema_identity(schema_name, schema_version)
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_method_spec_future_version_rejected():
    """MethodSpec.from_dict with schema_version=2 raises SCHEMA_UNSUPPORTED."""
    with pytest.raises(BridgeError) as exc_info:
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.method",
                "schema_version": 2,
                "mode": "photometry",
                "name": "x",
                "photometry": {
                    "filter_id": "P610",
                    "filter_name": "600nm",
                    "read_time_seconds": 1.0,
                },
            }
        )
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_method_spec_unknown_name_rejected():
    """MethodSpec.from_dict with unknown schema_name raises SCHEMA_UNSUPPORTED."""
    with pytest.raises(BridgeError) as exc_info:
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.future",
                "schema_version": 1,
                "mode": "photometry",
                "name": "x",
                "photometry": {
                    "filter_id": "P610",
                    "filter_name": "600nm",
                    "read_time_seconds": 1.0,
                },
            }
        )
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_layout_spec_future_version_rejected():
    """LayoutSpec.from_dict with schema_version=2 raises SCHEMA_UNSUPPORTED."""
    with pytest.raises(BridgeError) as exc_info:
        LayoutSpec.from_dict({"schema_name": "wallac.layout", "schema_version": 2, "wells": []})
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_analysis_spec_future_version_rejected():
    """AnalysisSpec.from_dict with schema_version=2 raises SCHEMA_UNSUPPORTED."""
    with pytest.raises(BridgeError) as exc_info:
        AnalysisSpec.from_dict({"schema_name": "wallac.analysis", "schema_version": 2})
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_job_spec_future_version_rejected():
    """JobSpec.from_dict with schema_version=2 raises SCHEMA_UNSUPPORTED."""
    with pytest.raises(BridgeError) as exc_info:
        JobSpec.from_dict(
            {
                "schema_name": "wallac.job",
                "schema_version": 2,
                "execution_mode": "existing_protocol",
            }
        )
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# 5. Round-trip tests
# ---------------------------------------------------------------------------


def test_method_spec_roundtrip():
    """MethodSpec → to_dict → from_dict → to_dict is equal."""
    from bridge.schemas import FluorometrySettings

    spec = MethodSpec(
        name="GFP",
        mode="fluorometry",
        fluorometry=FluorometrySettings(
            excitation_filter_id="F485",
            excitation_filter_name="485nm",
            emission_filter_id="F535",
            emission_filter_name="535nm",
            read_time_seconds=0.5,
        ),
    )
    d1 = spec.to_dict()
    spec2 = MethodSpec.from_dict(d1)
    d2 = spec2.to_dict()
    assert d1 == d2


def test_layout_spec_roundtrip():
    """LayoutSpec → to_dict → from_dict → to_dict is equal."""
    from bridge.schemas import WellSpec

    spec = LayoutSpec(
        plate_type="96-well",
        wells=[
            WellSpec(well_name="A1", role="measured", sample_name="S1"),
            WellSpec(well_name="A2", role="skipped"),
        ],
    )
    d1 = spec.to_dict()
    spec2 = LayoutSpec.from_dict(d1)
    d2 = spec2.to_dict()
    assert d1 == d2


def test_analysis_spec_roundtrip():
    """AnalysisSpec → to_dict → from_dict → to_dict is equal."""
    spec = AnalysisSpec()
    d1 = spec.to_dict()
    spec2 = AnalysisSpec.from_dict(d1)
    d2 = spec2.to_dict()
    assert d1 == d2


def test_job_spec_roundtrip():
    """JobSpec → to_dict → from_dict → to_dict is equal."""
    spec = JobSpec(execution_mode="existing_protocol", protocol_name="Test Protocol")
    d1 = spec.to_dict()
    spec2 = JobSpec.from_dict(d1)
    d2 = spec2.to_dict()
    assert d1 == d2


# ---------------------------------------------------------------------------
# 6. Well name validation
# ---------------------------------------------------------------------------


def test_valid_well_names_accepted():
    """is_valid_well_name returns True for valid 96-well names."""
    assert is_valid_well_name("A1") is True
    assert is_valid_well_name("A12") is True
    assert is_valid_well_name("H12") is True


def test_invalid_well_names_rejected():
    """is_valid_well_name returns False for names outside A1–H12."""
    assert is_valid_well_name("A13") is False
    assert is_valid_well_name("I1") is False
    assert is_valid_well_name("") is False


def test_well_spec_from_dict_invalid_name_raises():
    """WellSpec.from_dict with an invalid well name raises ValueError."""
    from bridge.schemas import WellSpec

    with pytest.raises(ValueError, match="Invalid well name"):
        WellSpec.from_dict({"well_name": "Z9", "role": "measured"})


def test_valid_well_names_count_and_order():
    """VALID_WELL_NAMES has 96 entries, first is A1, last is H12."""
    assert len(VALID_WELL_NAMES) == 96
    assert VALID_WELL_NAMES[0] == "A1"
    assert VALID_WELL_NAMES[-1] == "H12"


# ---------------------------------------------------------------------------
# 7. Mode validation
# ---------------------------------------------------------------------------


def test_method_spec_unsupported_mode_raises():
    """MethodSpec.from_dict with unsupported mode raises BridgeError(SCHEMA_UNSUPPORTED)."""
    with pytest.raises(BridgeError) as exc_info:
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.method",
                "schema_version": 1,
                "mode": "trf",
                "name": "Test",
            }
        )
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_method_spec_photometry_without_settings_raises():
    """Method with mode=photometry but no photometry settings raises ValueError."""
    with pytest.raises(ValueError, match="photometry settings required"):
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.method",
                "schema_version": 1,
                "mode": "photometry",
                "name": "Test",
            }
        )


def test_method_spec_fluorometry_without_settings_raises():
    """Method with mode=fluorometry but no fluorometry settings raises ValueError."""
    with pytest.raises(ValueError, match="fluorometry settings required"):
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.method",
                "schema_version": 1,
                "mode": "fluorometry",
                "name": "Test",
            }
        )


def test_method_spec_luminescence_without_settings_raises():
    """Method with mode=luminescence but no luminescence settings raises ValueError."""
    with pytest.raises(ValueError, match="luminescence settings required"):
        MethodSpec.from_dict(
            {
                "schema_name": "wallac.method",
                "schema_version": 1,
                "mode": "luminescence",
                "name": "Test",
            }
        )


# ---------------------------------------------------------------------------
# 8. Execution mode validation
# ---------------------------------------------------------------------------


def test_job_spec_unknown_execution_mode_raises():
    """JobSpec.from_dict with unknown execution mode raises BridgeError(SCHEMA_UNSUPPORTED)."""
    with pytest.raises(BridgeError) as exc_info:
        JobSpec.from_dict(
            {
                "schema_name": "wallac.job",
                "schema_version": 1,
                "execution_mode": "unknown",
            }
        )
    assert exc_info.value.code == SCHEMA_UNSUPPORTED


def test_generated_protocol_reusable_layout_without_object_id_raises():
    """Generated protocol job with reusable layout but no object_id raises ValueError."""
    from bridge.schemas import LayoutReference

    with pytest.raises(ValueError, match="reusable layout reference requires object_id"):
        LayoutReference.from_dict(
            {
                "source": "reusable",
                "hash": "abc123",
                "json_attachment_id": 100,
            }
        )


def test_one_off_layout_no_object_id_accepted():
    """A one-off layout reference does not require object_id and parses without error."""
    from bridge.schemas import LayoutReference

    ref = LayoutReference.from_dict(
        {
            "source": "one_off",
            "hash": "abc123",
            "json_attachment_id": 100,
        }
    )
    assert ref.source == "one_off"
    assert ref.object_id == 0


# ---------------------------------------------------------------------------
# 9. Canonical JSON properties
# ---------------------------------------------------------------------------


def test_canonicalize_has_no_whitespace():
    """canonicalize output has no spaces after , or :."""
    data = {"a": 1, "b": [2, 3], "c": {"d": "e"}}
    output = canonicalize(data)
    text = output.decode("utf-8")
    assert " " not in text
    assert ": " not in text
    assert ", " not in text


def test_canonicalize_sorted_keys():
    """canonicalize output has keys sorted at all nesting levels."""
    # Keys in non-alphabetical order.
    data = {"z": 1, "a": 2, "m": {"y": 3, "b": 4}}
    output = canonicalize(data)
    text = output.decode("utf-8")
    # Must start with "a" (first sorted key at top level).
    assert text.startswith('{"a":')
    # The inner object must also be sorted.
    assert '"b":4,"y":3' in text or '"b": 4, "y": 3' in text
    # More precise check: canonical JSON has no spaces.
    assert '"b":4,"y":3' in text


def test_canonicalize_preserves_utf8_characters():
    """canonicalize uses ensure_ascii=False so non-ASCII chars are UTF-8, not \\uXXXX."""
    data = {"name": "Jürgen Müller"}
    output = canonicalize(data)
    text = output.decode("utf-8")
    assert "Jürgen Müller" in text
    assert "\\u" not in text
