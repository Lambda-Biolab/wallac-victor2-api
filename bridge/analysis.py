"""Analysis pipeline for Wallac Victor2 results.

Implements the analysis portion of Stage 6 of
docs/plans/wallac-protocol-authoring.md.

Runs in the Linux-side bridge, not inside the Windows vm-agent. Applies
signed ``analysis.json`` to raw per-well results and produces raw/analyzed
artifacts.

Fixed v1 analysis pipeline order:
1. load raw per-well values
2. mark skipped/unmeasured wells
3. apply analysis exclusions
4. compute blank from non-excluded blank wells
5. subtract blank where configured
6. compute normalization factor from control wells/groups
7. apply normalization where configured
8. aggregate replicate groups: mean, SD, CV, N
9. apply thresholds/pass-fail rules
10. emit raw, analyzed per-well, replicate summary, and analysis summary artifacts
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .schemas import AnalysisSpec, WellRole

logger = logging.getLogger(__name__)


def _check_threshold(operator: str, value: float, threshold: float) -> bool:
    """Return True if the threshold rule is violated (i.e., value fails the check)."""
    ops: dict[str, Any] = {
        ">=": lambda v, t: v < t,
        "<=": lambda v, t: v > t,
        ">": lambda v, t: v <= t,
        "<": lambda v, t: v >= t,
        "==": lambda v, t: v != t,
        "!=": lambda v, t: v == t,
    }
    check = ops.get(operator)
    return check(value, threshold) if check else False


@dataclass
class WellResult:
    """A single well's raw and analyzed result."""

    well_name: str
    role: str = WellRole.MEASURED.value
    raw_value: float | None = None
    primary_value: float | None = None
    blank_subtracted: float | None = None
    normalized: float | None = None
    replicate_group: str = ""
    control_type: str = ""
    sample_name: str = ""
    excluded: bool = False
    exclusion_reason: str = ""
    measurement_status: str = "measured"  # "measured", "skipped"
    analysis_excluded: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "well_name": self.well_name,
            "role": self.role,
            "measurement_status": self.measurement_status,
        }
        if self.raw_value is not None:
            d["raw_value"] = self.raw_value
        if self.primary_value is not None:
            d["primary_value"] = self.primary_value
        if self.blank_subtracted is not None:
            d["blank_subtracted"] = self.blank_subtracted
        if self.normalized is not None:
            d["normalized"] = self.normalized
        if self.replicate_group:
            d["replicate_group"] = self.replicate_group
        if self.control_type:
            d["control_type"] = self.control_type
        if self.sample_name:
            d["sample_name"] = self.sample_name
        if self.excluded or self.analysis_excluded:
            d["analysis_excluded"] = True
            if self.exclusion_reason:
                d["exclusion_reason"] = self.exclusion_reason
        return d


@dataclass
class ReplicateGroup:
    """Aggregated statistics for a replicate group."""

    group_name: str
    mean: float = 0.0
    sd: float = 0.0
    cv: float = 0.0
    n: int = 0
    wells: list[str] = field(default_factory=list)
    pass_fail: str = "pass"  # "pass" or "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_name": self.group_name,
            "mean": round(self.mean, 6),
            "sd": round(self.sd, 6),
            "cv": round(self.cv, 4),
            "n": self.n,
            "wells": list(self.wells),
            "pass_fail": self.pass_fail,
        }


@dataclass
class AnalysisResult:
    """Complete analysis output for a job."""

    wells: list[WellResult] = field(default_factory=list)
    replicate_groups: list[ReplicateGroup] = field(default_factory=list)
    blank_value: float | None = None
    normalization_factor: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    pass_fail: str = "pass"

    def to_analyzed_wells_csv(self) -> str:
        """Export analyzed per-well results as CSV."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "well_name",
                "role",
                "measurement_status",
                "raw_value",
                "primary_value",
                "blank_subtracted",
                "normalized",
                "replicate_group",
                "control_type",
                "sample_name",
                "analysis_excluded",
                "exclusion_reason",
            ]
        )
        for w in self.wells:
            writer.writerow(
                [
                    w.well_name,
                    w.role,
                    w.measurement_status,
                    w.raw_value if w.raw_value is not None else "",
                    w.primary_value if w.primary_value is not None else "",
                    w.blank_subtracted if w.blank_subtracted is not None else "",
                    w.normalized if w.normalized is not None else "",
                    w.replicate_group,
                    w.control_type,
                    w.sample_name,
                    w.analysis_excluded,
                    w.exclusion_reason,
                ]
            )
        return output.getvalue()

    def to_replicate_summary_csv(self) -> str:
        """Export replicate group summary as CSV."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["group_name", "mean", "sd", "cv", "n", "wells", "pass_fail"])
        for g in self.replicate_groups:
            writer.writerow(
                [
                    g.group_name,
                    g.mean,
                    g.sd,
                    g.cv,
                    g.n,
                    ";".join(g.wells),
                    g.pass_fail,
                ]
            )
        return output.getvalue()

    def to_replicate_summary_json(self) -> str:
        """Export replicate group summary as JSON."""
        return json.dumps(
            [g.to_dict() for g in self.replicate_groups],
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_analysis_summary_json(self) -> str:
        """Export analysis summary as JSON."""
        return json.dumps(self.summary, sort_keys=True, separators=(",", ":"))


class AnalysisPipeline:
    """Executes the fixed v1 analysis pipeline on raw results.

    All operations work on the ``primary_value`` abstraction:
    - photometry stores OD plus raw counts/signals; primary_value prefers OEM OD
    - fluorometry/luminescence store raw intensity/counts as primary_value
    """

    def run(
        self,
        raw_wells: list[dict[str, Any]],
        layout_wells: dict[str, dict[str, Any]],
        spec: AnalysisSpec,
    ) -> AnalysisResult:
        """Run the full analysis pipeline.

        Args:
            raw_wells: Raw per-well results from the instrument.
                Each dict has at least ``well_name`` and ``primary_value``.
            layout_wells: Well definitions from the layout spec, keyed by well name.
            spec: The signed analysis plan specification.

        Returns:
            AnalysisResult with all wells, replicate groups, and summary.
        """
        result = AnalysisResult()

        # Step 1-2: Load raw values and mark skipped wells
        self._load_raw(raw_wells, layout_wells, result)

        # Step 3: Apply exclusions
        self._apply_exclusions(result, spec.exclusions)

        # Step 4-5: Blank subtraction
        if spec.blank_subtraction.enabled:
            self._subtract_blank(result, spec.blank_subtraction.blank_wells)

        # Step 6-7: Normalization
        if spec.normalization.enabled:
            self._normalize(result, spec.normalization)

        # Step 8: Aggregate replicates
        if spec.replicate_aggregation.enabled:
            self._aggregate_replicates(result, spec.replicate_aggregation.group_by)

        # Step 9: Apply thresholds
        self._apply_thresholds(result, spec.thresholds)

        # Step 10: Build summary
        self._build_summary(result, spec)

        return result

    def _load_raw(
        self,
        raw_wells: list[dict[str, Any]],
        layout_wells: dict[str, dict[str, Any]],
        result: AnalysisResult,
    ) -> None:
        """Steps 1-2: Load raw values, mark skipped wells, merge layout info."""
        # vm-agent returns 'well', layout/analysis specs use 'well_name'
        raw_by_name = {
            (w.get("well_name") or w.get("well") or ""): w for w in raw_wells
        }

        # Include all 96 wells from the layout
        for well_name, layout_def in layout_wells.items():
            role = layout_def.get("role", WellRole.MEASURED.value)
            raw = raw_by_name.get(well_name, {})

            well = WellResult(
                well_name=well_name,
                role=role,
                raw_value=raw.get("primary_value") or raw.get("od") or raw.get("counts"),
                replicate_group=layout_def.get("replicate_group", ""),
                control_type=layout_def.get("control_type", ""),
                sample_name=layout_def.get("sample_name", ""),
            )

            if role == WellRole.SKIPPED.value:
                well.measurement_status = "skipped"
            else:
                well.measurement_status = "measured"
                well.primary_value = well.raw_value

            if role == WellRole.EXCLUDED.value:
                well.excluded = True
                well.exclusion_reason = "layout_excluded"

            result.wells.append(well)

        # Also include any raw wells not in the layout (unexpected)
        for well_name, raw in raw_by_name.items():
            if well_name not in layout_wells:
                result.wells.append(
                    WellResult(
                        well_name=well_name,
                        role=WellRole.MEASURED.value,
                        raw_value=raw.get("primary_value") or raw.get("od") or raw.get("counts"),
                        primary_value=raw.get("primary_value")
                        or raw.get("od")
                        or raw.get("counts"),
                        measurement_status="measured",
                    )
                )

    def _apply_exclusions(self, result: AnalysisResult, exclusions: list[str]) -> None:
        """Step 3: Apply analysis-level exclusions."""
        exclusion_set = set(exclusions)
        for well in result.wells:
            if well.well_name in exclusion_set:
                well.analysis_excluded = True
                if not well.exclusion_reason:
                    well.exclusion_reason = "analysis_excluded"

    def _subtract_blank(self, result: AnalysisResult, blank_wells: list[str]) -> None:
        """Steps 4-5: Compute blank from non-excluded blank wells and subtract."""
        blank_set = set(blank_wells)
        blank_values = [
            w.primary_value
            for w in result.wells
            if w.well_name in blank_set
            and not w.analysis_excluded
            and not w.excluded
            and w.primary_value is not None
        ]

        if not blank_values:
            result.blank_value = None
            return

        blank = sum(blank_values) / len(blank_values)
        result.blank_value = blank

        for well in result.wells:
            if (
                well.measurement_status == "measured"
                and not well.analysis_excluded
                and not well.excluded
                and well.primary_value is not None
            ):
                well.blank_subtracted = well.primary_value - blank

    def _normalize(self, result: AnalysisResult, norm_config: Any) -> None:
        """Steps 6-7: Compute normalization factor from controls and apply."""
        control_type = norm_config.control_type
        target = norm_config.target_value

        control_values = [
            w.blank_subtracted if w.blank_subtracted is not None else w.primary_value
            for w in result.wells
            if w.control_type == control_type
            and not w.analysis_excluded
            and not w.excluded
            and (w.blank_subtracted is not None or w.primary_value is not None)
        ]

        if not control_values:
            result.normalization_factor = None
            return

        mean_control = sum(control_values) / len(control_values)
        if mean_control == 0:
            result.normalization_factor = None
            return

        factor = target / mean_control
        result.normalization_factor = factor

        for well in result.wells:
            if (
                well.measurement_status == "measured"
                and not well.analysis_excluded
                and not well.excluded
            ):
                base = (
                    well.blank_subtracted
                    if well.blank_subtracted is not None
                    else well.primary_value
                )
                if base is not None:
                    well.normalized = base * factor

    def _aggregate_replicates(self, result: AnalysisResult, group_by: str) -> None:
        """Step 8: Aggregate replicate groups: mean, SD, CV, N."""
        groups: dict[str, list[WellResult]] = {}

        for well in result.wells:
            if well.measurement_status != "measured" or well.analysis_excluded or well.excluded:
                continue

            group_key = getattr(well, group_by, "") or well.well_name
            if not group_key:
                group_key = well.well_name

            groups.setdefault(group_key, []).append(well)

        for group_name, wells in sorted(groups.items()):
            values = [
                w.normalized
                if w.normalized is not None
                else w.blank_subtracted
                if w.blank_subtracted is not None
                else w.primary_value
                for w in wells
                if (
                    w.normalized is not None
                    or w.blank_subtracted is not None
                    or w.primary_value is not None
                )
            ]

            if not values:
                continue

            n = len(values)
            mean = sum(values) / n
            sd = math.sqrt(sum((v - mean) ** 2 for v in values) / n) if n > 1 else 0.0
            cv = abs(sd / mean * 100) if mean != 0 else 0.0

            result.replicate_groups.append(
                ReplicateGroup(
                    group_name=group_name,
                    mean=mean,
                    sd=sd,
                    cv=cv,
                    n=n,
                    wells=[w.well_name for w in wells],
                )
            )

    def _apply_thresholds(self, result: AnalysisResult, thresholds: list[Any]) -> None:
        """Step 9: Apply pass/fail threshold rules."""
        for rule in thresholds:
            for group in result.replicate_groups:
                metric_value = getattr(group, rule.metric, None)
                if metric_value is None:
                    continue

                if _check_threshold(rule.operator, metric_value, rule.value):
                    if rule.action == "fail":
                        group.pass_fail = "fail"
                        result.pass_fail = "fail"
                    elif rule.action == "flag":
                        group.pass_fail = "flag"

    def _build_summary(self, result: AnalysisResult, spec: AnalysisSpec) -> None:
        """Step 10: Build analysis summary."""
        measured = [w for w in result.wells if w.measurement_status == "measured"]
        excluded = [w for w in result.wells if w.analysis_excluded or w.excluded]
        skipped = [w for w in result.wells if w.measurement_status == "skipped"]

        result.summary = {
            "total_wells": len(result.wells),
            "measured_wells": len(measured),
            "excluded_wells": len(excluded),
            "skipped_wells": len(skipped),
            "replicate_groups": len(result.replicate_groups),
            "blank_value": result.blank_value,
            "normalization_factor": result.normalization_factor,
            "pass_fail": result.pass_fail,
            "thresholds_checked": len(spec.thresholds),
        }
