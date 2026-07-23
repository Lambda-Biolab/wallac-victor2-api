"""Property tests for data contracts and safety-critical invariants."""

from __future__ import annotations

import json
import re

from hypothesis import given
from hypothesis import strategies as st

from bridge.canonical import canonicalize, compute_hash
from bridge.jobs import dedup_key
from bridge.schemas import ControlType, WellRole, WellSpec
from bridge.well_utils import normalize_well_name

json_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20)
json_leaf = (
    st.none() | st.booleans() | st.integers(min_value=-(2**63), max_value=2**63 - 1) | json_text
)
json_value = st.recursive(
    json_leaf,
    lambda child: st.lists(child, max_size=5) | st.dictionaries(json_text, child, max_size=5),
    max_leaves=20,
)
json_object = st.dictionaries(json_text, json_value, max_size=5)


@given(json_object)
def test_canonical_json_is_stable_and_round_trips(data: dict[str, object]) -> None:
    canonical = canonicalize(data)

    assert json.loads(canonical) == data
    assert canonicalize(json.loads(canonical)) == canonical
    assert re.fullmatch(r"[0-9a-f]{64}", compute_hash(canonical))


@given(json_text)
def test_well_normalization_is_idempotent(value: str) -> None:
    normalized = normalize_well_name(value)
    assert normalize_well_name(normalized) == normalized


@given(
    row=st.sampled_from(tuple("ABCDEFGH")),
    column=st.integers(min_value=1, max_value=12),
    lowercase=st.booleans(),
    zero_padded=st.booleans(),
    padding=st.sampled_from(("", " ", "  ")),
)
def test_well_variants_normalize_to_canonical_name(
    row: str,
    column: int,
    lowercase: bool,
    zero_padded: bool,
    padding: str,
) -> None:
    rendered_row = row.lower() if lowercase else row
    rendered_column = f"{column:02d}" if zero_padded else str(column)
    value = f"{padding}{rendered_row}{rendered_column}{padding}"

    assert normalize_well_name(value) == f"{row}{column}"


@given(
    mode=st.sampled_from(("existing_protocol", "generated_protocol")),
    protocol_name=json_text,
    object_id=st.integers(min_value=0, max_value=1_000_000),
    attachment_id=st.integers(min_value=0, max_value=1_000_000),
    hash_value=st.text(alphabet="0123456789abcdef", min_size=0, max_size=64),
)
def test_job_dedup_key_ignores_mapping_order(
    mode: str,
    protocol_name: str,
    object_id: int,
    attachment_id: int,
    hash_value: str,
) -> None:
    method_ref = {
        "object_id": object_id,
        "json_attachment_id": attachment_id,
        "hash": hash_value,
    }
    spec = {
        "execution_mode": mode,
        "protocol_name": protocol_name,
        "method_ref": method_ref,
        "layout_ref": {},
        "analysis_ref": {},
    }
    reversed_spec = dict(reversed(tuple(spec.items())))

    assert dedup_key(spec) == dedup_key(reversed_spec)


def test_generated_job_dedup_ignores_irrelevant_protocol_identity() -> None:
    refs = {
        "method_ref": {"object_id": 1, "json_attachment_id": 2, "hash": "a" * 64},
        "layout_ref": {"object_id": 3, "json_attachment_id": 4, "hash": "b" * 64},
        "analysis_ref": {"object_id": 5, "json_attachment_id": 6, "hash": "c" * 64},
    }
    first = {
        "execution_mode": "generated_protocol",
        "protocol_id": 1001,
        "protocol_name": "Ignored A",
        **refs,
    }
    second = {
        "execution_mode": "generated_protocol",
        "protocol_id": 2002,
        "protocol_name": "Ignored B",
        **refs,
    }

    assert dedup_key(first) == dedup_key(second)


@given(
    row=st.sampled_from(tuple("ABCDEFGH")),
    column=st.integers(min_value=1, max_value=12),
    role=st.sampled_from(tuple(role.value for role in WellRole)),
    sample_name=json_text,
    sample_label=json_text,
    replicate_group=json_text,
    control_type=st.sampled_from(("", *(control.value for control in ControlType))),
    item_id=st.integers(min_value=0, max_value=1_000_000),
)
def test_well_spec_round_trip_preserves_valid_values(
    row: str,
    column: int,
    role: str,
    sample_name: str,
    sample_label: str,
    replicate_group: str,
    control_type: str,
    item_id: int,
) -> None:
    spec = WellSpec(
        well_name=f"{row}{column}",
        role=role,
        sample_name=sample_name,
        sample_label=sample_label,
        replicate_group=replicate_group,
        control_type=control_type,
        elabftw_item_id=item_id,
    )

    assert WellSpec.from_dict(spec.to_dict()) == spec
