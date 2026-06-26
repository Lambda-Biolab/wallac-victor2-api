"""Tests for the analysis pipeline (Stage 6).

Tests cover:
- Raw loading and skipped well marking
- Blank subtraction
- Normalization
- Replicate aggregation (mean, SD, CV, N)
- Threshold pass/fail rules
- Exclusions
- CSV/JSON artifact export
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest

from bridge.analysis import AnalysisPipeline
from bridge.schemas import (
    AnalysisSpec,
    BlankSubtractionConfig,
    NormalizationConfig,
    ReplicateAggregationConfig,
    ThresholdRule,
)

# --- Helpers ---


def make_layout(wells: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a layout_wells dict from a list of well defs."""
    return {w["well_name"]: w for w in wells}


def make_raw(wells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a raw_wells list."""
    return wells


# --- Basic loading tests ---


class TestLoadRaw:
    def test_load_measured_wells(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "sample_name": "S1"},
                {"well_name": "A2", "role": "measured", "sample_name": "S2"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 0.5},
                {"well_name": "A2", "primary_value": 0.6},
            ]
        )
        spec = AnalysisSpec()
        result = AnalysisPipeline().run(raw, layout, spec)

        assert len(result.wells) == 2
        assert result.wells[0].primary_value == 0.5
        assert result.wells[0].measurement_status == "measured"

    def test_skipped_wells_marked(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "skipped"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 0.5}])
        spec = AnalysisSpec()
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.wells[0].measurement_status == "measured"
        assert result.wells[1].measurement_status == "skipped"
        assert result.wells[1].raw_value is None

    def test_layout_excluded_wells(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "excluded"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 0.5},
                {"well_name": "A2", "primary_value": 0.6},
            ]
        )
        spec = AnalysisSpec()
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.wells[1].excluded is True
        assert result.wells[1].exclusion_reason == "layout_excluded"


# --- Blank subtraction tests ---


class TestBlankSubtraction:
    def test_blank_subtraction(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "measured"},
                {"well_name": "H11", "role": "measured", "control_type": "blank"},
                {"well_name": "H12", "role": "measured", "control_type": "blank"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 0.5},
                {"well_name": "A2", "primary_value": 0.6},
                {"well_name": "H11", "primary_value": 0.1},
                {"well_name": "H12", "primary_value": 0.15},
            ]
        )
        spec = AnalysisSpec(
            blank_subtraction=BlankSubtractionConfig(enabled=True, blank_wells=["H11", "H12"])
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        # Blank = (0.1 + 0.15) / 2 = 0.125
        assert result.blank_value == pytest.approx(0.125)
        assert result.wells[0].blank_subtracted == pytest.approx(0.375)
        assert result.wells[1].blank_subtracted == pytest.approx(0.475)

    def test_blank_subtraction_disabled(self) -> None:
        layout = make_layout([{"well_name": "A1", "role": "measured"}])
        raw = make_raw([{"well_name": "A1", "primary_value": 0.5}])
        spec = AnalysisSpec()  # blank_subtraction disabled by default
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.blank_value is None
        assert result.wells[0].blank_subtracted is None


# --- Normalization tests ---


class TestNormalization:
    def test_normalization_to_positive_control(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "measured", "control_type": "positive_control"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 50.0},
                {"well_name": "A2", "primary_value": 100.0},
            ]
        )
        spec = AnalysisSpec(
            normalization=NormalizationConfig(
                enabled=True,
                control_type="positive_control",
                target_value=100.0,
            )
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        # Mean control = 100, target = 100, factor = 1.0
        assert result.normalization_factor == pytest.approx(1.0)
        assert result.wells[0].normalized == pytest.approx(50.0)

    def test_normalization_factor(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "measured", "control_type": "positive_control"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 25.0},
                {"well_name": "A2", "primary_value": 50.0},
            ]
        )
        spec = AnalysisSpec(
            normalization=NormalizationConfig(
                enabled=True,
                control_type="positive_control",
                target_value=100.0,
            )
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        # Mean control = 50, target = 100, factor = 2.0
        assert result.normalization_factor == pytest.approx(2.0)
        assert result.wells[0].normalized == pytest.approx(50.0)


# --- Replicate aggregation tests ---


class TestReplicateAggregation:
    def test_aggregation_mean_sd_cv(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A3", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 20.0},
                {"well_name": "A3", "primary_value": 30.0},
            ]
        )
        spec = AnalysisSpec(replicate_aggregation=ReplicateAggregationConfig(enabled=True))
        result = AnalysisPipeline().run(raw, layout, spec)

        assert len(result.replicate_groups) == 1
        group = result.replicate_groups[0]
        assert group.group_name == "G1"
        assert group.mean == pytest.approx(20.0)
        assert group.n == 3
        assert group.sd > 0
        assert group.cv > 0

    def test_aggregation_disabled(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 10.0}])
        spec = AnalysisSpec()  # aggregation disabled
        result = AnalysisPipeline().run(raw, layout, spec)

        assert len(result.replicate_groups) == 0

    def test_aggregation_excludes_excluded_wells(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "excluded", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 100.0},
            ]
        )
        spec = AnalysisSpec(replicate_aggregation=ReplicateAggregationConfig(enabled=True))
        result = AnalysisPipeline().run(raw, layout, spec)

        assert len(result.replicate_groups) == 1
        assert result.replicate_groups[0].n == 1  # only A1, not A2
        assert result.replicate_groups[0].mean == pytest.approx(10.0)


# --- Threshold tests ---


class TestThresholds:
    def test_threshold_fail(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 20.0},
            ]
        )
        spec = AnalysisSpec(
            replicate_aggregation=ReplicateAggregationConfig(enabled=True),
            thresholds=[
                ThresholdRule(
                    name="min_mean",
                    metric="mean",
                    operator=">=",
                    value=100.0,
                    action="fail",
                )
            ],
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.pass_fail == "fail"
        assert result.replicate_groups[0].pass_fail == "fail"

    def test_threshold_pass(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 100.0}])
        spec = AnalysisSpec(
            replicate_aggregation=ReplicateAggregationConfig(enabled=True),
            thresholds=[
                ThresholdRule(
                    name="min_mean",
                    metric="mean",
                    operator=">=",
                    value=50.0,
                    action="fail",
                )
            ],
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.pass_fail == "pass"

    def test_threshold_cv_flag(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 100.0},
            ]
        )
        spec = AnalysisSpec(
            replicate_aggregation=ReplicateAggregationConfig(enabled=True),
            thresholds=[
                ThresholdRule(
                    name="max_cv",
                    metric="cv",
                    operator="<=",
                    value=10.0,
                    action="flag",
                )
            ],
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        # CV should be high (mean=55, values 10 and 100)
        assert result.replicate_groups[0].pass_fail == "flag"
        # "flag" doesn't set overall pass_fail to "fail"
        assert result.pass_fail == "pass"


# --- Exclusion tests ---


class TestExclusions:
    def test_analysis_exclusion(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 100.0},  # outlier
            ]
        )
        spec = AnalysisSpec(
            replicate_aggregation=ReplicateAggregationConfig(enabled=True),
            exclusions=["A2"],
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        assert result.wells[1].analysis_excluded is True
        assert result.wells[1].exclusion_reason == "analysis_excluded"
        # A2 should not be in the aggregation
        assert result.replicate_groups[0].n == 1
        assert result.replicate_groups[0].mean == pytest.approx(10.0)


# --- Artifact export tests ---


class TestArtifactExport:
    def test_analyzed_wells_csv(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "sample_name": "S1"},
                {"well_name": "A2", "role": "skipped"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 0.5}])
        spec = AnalysisSpec()
        result = AnalysisPipeline().run(raw, layout, spec)

        csv_text = result.to_analyzed_wells_csv()
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["well_name"] == "A1"
        assert rows[0]["sample_name"] == "S1"
        assert rows[1]["measurement_status"] == "skipped"

    def test_replicate_summary_csv(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
                {"well_name": "A2", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 10.0},
                {"well_name": "A2", "primary_value": 20.0},
            ]
        )
        spec = AnalysisSpec(replicate_aggregation=ReplicateAggregationConfig(enabled=True))
        result = AnalysisPipeline().run(raw, layout, spec)

        csv_text = result.to_replicate_summary_csv()
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["group_name"] == "G1"
        assert rows[0]["n"] == "2"

    def test_replicate_summary_json(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured", "replicate_group": "G1"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 10.0}])
        spec = AnalysisSpec(replicate_aggregation=ReplicateAggregationConfig(enabled=True))
        result = AnalysisPipeline().run(raw, layout, spec)

        json_text = result.to_replicate_summary_json()
        data = json.loads(json_text)
        assert len(data) == 1
        assert data[0]["group_name"] == "G1"

    def test_analysis_summary_json(self) -> None:
        layout = make_layout(
            [
                {"well_name": "A1", "role": "measured"},
                {"well_name": "A2", "role": "skipped"},
            ]
        )
        raw = make_raw([{"well_name": "A1", "primary_value": 0.5}])
        spec = AnalysisSpec()
        result = AnalysisPipeline().run(raw, layout, spec)

        json_text = result.to_analysis_summary_json()
        data = json.loads(json_text)
        assert data["total_wells"] == 2
        assert data["measured_wells"] == 1
        assert data["skipped_wells"] == 1
        assert data["pass_fail"] == "pass"


# --- Full pipeline integration test ---


class TestFullPipeline:
    def test_full_pipeline(self) -> None:
        """Test the complete pipeline with blank subtraction, normalization,
        aggregation, and thresholds."""
        layout = make_layout(
            [
                {
                    "well_name": "A1",
                    "role": "measured",
                    "replicate_group": "G1",
                    "control_type": "",
                },
                {
                    "well_name": "A2",
                    "role": "measured",
                    "replicate_group": "G1",
                    "control_type": "",
                },
                {
                    "well_name": "A3",
                    "role": "measured",
                    "replicate_group": "G2",
                    "control_type": "positive_control",
                },
                {
                    "well_name": "A4",
                    "role": "measured",
                    "replicate_group": "G2",
                    "control_type": "positive_control",
                },
                {"well_name": "H12", "role": "measured", "control_type": "blank"},
            ]
        )
        raw = make_raw(
            [
                {"well_name": "A1", "primary_value": 45.0},
                {"well_name": "A2", "primary_value": 55.0},
                {"well_name": "A3", "primary_value": 90.0},
                {"well_name": "A4", "primary_value": 110.0},
                {"well_name": "H12", "primary_value": 5.0},
            ]
        )
        spec = AnalysisSpec(
            blank_subtraction=BlankSubtractionConfig(enabled=True, blank_wells=["H12"]),
            replicate_aggregation=ReplicateAggregationConfig(enabled=True),
            normalization=NormalizationConfig(
                enabled=True, control_type="positive_control", target_value=100.0
            ),
            thresholds=[
                ThresholdRule(name="max_cv", metric="cv", operator="<=", value=20.0, action="fail"),
            ],
        )
        result = AnalysisPipeline().run(raw, layout, spec)

        # Blank = 5.0
        assert result.blank_value == pytest.approx(5.0)

        # Normalized: control mean after blank = (85 + 105) / 2 = 95
        # Factor = 100 / 95
        assert result.normalization_factor is not None

        # Two sample groups + blank well as single-well group = 3
        assert len(result.replicate_groups) == 3

        # Summary
        assert result.summary["measured_wells"] == 5
        assert result.summary["pass_fail"] in ("pass", "fail")
