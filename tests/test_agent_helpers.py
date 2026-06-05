"""Unit tests for the pure helpers in vm-agent/agent.py (the data-shaping
logic most prone to bugs -- background parsing, OD computation, plate grid).
COM/HTTP paths need Windows + the instrument and are not covered here."""

import math

import agent
import pytest


def test_parse_backgrounds_basic():
    assert agent._parse_backgrounds("1,0#313706,2;") == {(1, 0): (313706.0, 2.0)}


def test_parse_backgrounds_multi_and_garbage():
    bg = agent._parse_backgrounds("1,0#100,2; 2,1#200,4 ; junk ; ")
    assert bg[(1, 0)] == (100.0, 2.0)
    assert bg[(2, 1)] == (200.0, 4.0)
    assert len(bg) == 2


def test_parse_backgrounds_empty():
    assert agent._parse_backgrounds("") == {}
    assert agent._parse_backgrounds(None) == {}


def test_od_matches_count_rate_formula():
    assert agent._od(284932, 2, (313706.0, 2.0)) == round(
        math.log10((313706.0 / 2.0) / (284932 / 2)), 4
    )


def test_od_guards():
    assert agent._od(0, 2, (313706.0, 2.0)) is None  # zero signal
    assert agent._od(284932, 2, None) is None  # no background
    assert agent._od(None, 2, (1.0, 1.0)) is None  # no reading


def test_grid_csv_raw_shape_and_placement():
    wells = [
        {"well": "A01", "meas_a": 100, "od": 0.5},
        {"well": "H12", "meas_a": 200, "od": 0.9},
    ]
    lines = agent._grid_csv(wells, "raw").strip().split("\n")
    assert lines[0] == "row,1,2,3,4,5,6,7,8,9,10,11,12"
    assert len(lines) == 9  # header + rows A..H
    assert lines[1].split(",") == ["A", "100"] + [""] * 11  # A01 -> col 1
    assert lines[8].split(",")[-1] == "200"  # H12 -> col 12


def test_grid_csv_od_and_empty_rows():
    out = agent._grid_csv([{"well": "B02", "meas_a": 5, "od": 0.123}], "od")
    rows = {ln.split(",")[0]: ln.split(",") for ln in out.strip().split("\n")[1:]}
    assert rows["B"][2] == "0.123"  # B02 -> col 2 (od value)
    assert rows["A"] == ["A"] + [""] * 12  # untouched row is blank


# --- user-friendly layer: result shaping ----------------------------------


def test_dedup_wells_prefers_od_and_maps_counts():
    wells = [
        {"well": "A01", "result_type": 0, "meas_a": 100},  # no od
        {"well": "A01", "result_type": 3, "meas_a": 100, "od": 0.07},
        {"well": "A02", "counts": 50, "od": 0.2},
    ]
    out = agent._dedup_wells(wells)
    assert len(out) == 2
    a01 = next(w for w in out if w["well"] == "A01")
    assert a01["od"] == 0.07
    assert a01["counts"] == 100  # counts falls back to meas_a


def test_format_results_list_and_grid():
    raw = [
        {"well": "A01", "meas_a": 100, "od": 0.07},
        {"well": "A01", "meas_a": 100, "od": 0.07, "result_type": 0},
    ]
    out = agent._format_results(raw, "persisted", shape="grid", value="od", dedup=True)
    assert out["source"] == "persisted"
    assert out["well_count"] == 1
    assert out["grid"]["A01"] == 0.07


def test_norm_grid_csv_placement():
    csv = agent._norm_grid_csv(
        [{"well": "A01", "od": 0.5, "counts": 100}, {"well": "H12", "od": 0.9, "counts": 200}],
        "od",
    )
    lines = csv.strip().split("\n")
    assert lines[0] == "row,1,2,3,4,5,6,7,8,9,10,11,12"
    assert lines[1].split(",")[1] == "0.5"  # A01 -> col 1
    assert lines[8].split(",")[-1] == "0.9"  # H12 -> col 12


# --- friendly error classification ----------------------------------------


def test_classify_instrument_not_ready():
    e = agent._classify_exc(AttributeError("'NoneType' object has no attribute 'GetJobID'"))
    assert e.status == 409
    assert e.code == "instrument_not_ready"


def test_classify_not_connected_and_busy():
    assert agent._classify_exc(RuntimeError("instrument not connected")).code == (
        "instrument_not_connected"
    )
    assert agent._classify_exc(RuntimeError("already running a measurement")).code == (
        "instrument_busy"
    )


def test_classify_timeout_internal_and_passthrough():
    assert agent._classify_exc(TimeoutError("x")).status == 504
    assert agent._classify_exc(ValueError("bad request")).status == 500  # not a COM fault
    orig = agent.ApiError(409, "instrument_busy", "hint")
    assert agent._classify_exc(orig) is orig  # ApiError passes through unchanged


# --- protocol resolution by name or id ------------------------------------


class _FakeWorker:
    def call(self, fn, timeout=None):
        return fn(None)


def test_resolve_protocol_by_id_name_substring(monkeypatch):
    protos = [
        {"id": 2000000, "name": "Absorbance @ 600 (1.0s)"},
        {"id": 1000003, "name": "Absorbance @ 405 (1.0s)"},
    ]
    monkeypatch.setattr(agent, "_protocols_cache", protos)
    w = _FakeWorker()
    assert agent._resolve_protocol(2000000, w)["id"] == 2000000
    assert agent._resolve_protocol("Absorbance @ 600 (1.0s)", w)["id"] == 2000000  # exact
    assert agent._resolve_protocol("@ 600", w)["id"] == 2000000  # unique substring


def test_resolve_protocol_missing_and_ambiguous(monkeypatch):
    protos = [
        {"id": 1, "name": "Absorbance @ 405 (1.0s)"},
        {"id": 2, "name": "Absorbance @ 450 (1.0s)"},
    ]
    monkeypatch.setattr(agent, "_protocols_cache", protos)
    w = _FakeWorker()
    with pytest.raises(agent.ApiError) as e1:
        agent._resolve_protocol("nope", w)
    assert e1.value.status == 404
    with pytest.raises(agent.ApiError) as e2:
        agent._resolve_protocol("Absorbance", w)  # matches both
    assert e2.value.status == 409
