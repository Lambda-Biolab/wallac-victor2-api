"""Unit tests for the pure helpers in vm-agent/agent.py (the data-shaping
logic most prone to bugs -- background parsing, OD computation, plate grid).
COM/HTTP paths need Windows + the instrument and are not covered here."""

import math

import agent


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
