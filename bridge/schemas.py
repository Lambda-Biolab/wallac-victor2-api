"""Canonical JSON schema definitions for Wallac Victor2 protocol authoring.

Defines the four v1 schema types as dataclasses with ``to_dict()`` (for
canonicalization via :mod:`bridge.canonical`) and ``from_dict()`` (for
parsing signed attachments after hash verification).

Supported v1 schema names (full identifier includes ``.v1`` suffix):

  - ``wallac.method.v1``
  - ``wallac.layout.v1``
  - ``wallac.analysis.v1``
  - ``wallac.job.v1``

The bridge accepts only explicitly supported schema versions.  Unknown or
future versions fail closed with ``SCHEMA_UNSUPPORTED``.  Schema migrations
create new draft objects/attachments and new signatures; they never silently
convert signed JSON in place.

Source: docs/plans/wallac-protocol-authoring.md "Canonical JSON contracts"
        and "Per-mode constraints".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .errors import SCHEMA_UNSUPPORTED, BridgeError

# --- Schema identity -------------------------------------------------------

SCHEMA_VERSION_V1 = 1

SUPPORTED_SCHEMAS: dict[str, set[int]] = {
    "wallac.method": {SCHEMA_VERSION_V1},
    "wallac.layout": {SCHEMA_VERSION_V1},
    "wallac.analysis": {SCHEMA_VERSION_V1},
    "wallac.job": {SCHEMA_VERSION_V1},
}


def full_schema_name(name: str, version: int) -> str:
    """Return the dotted schema identifier, e.g. ``wallac.method.v1``."""
    return f"{name}.v{version}"


def validate_schema_identity(schema_name: str, schema_version: int) -> None:
    """Fail closed if the schema name or version is not explicitly supported.

    Raises:
        BridgeError: with code ``SCHEMA_UNSUPPORTED`` if the name is unknown
            or the version is not in the supported set for that name.
    """
    supported_versions = SUPPORTED_SCHEMAS.get(schema_name)
    if supported_versions is None:
        raise BridgeError(
            code=SCHEMA_UNSUPPORTED,
            human_message=(
                f"Unsupported schema name '{schema_name}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_SCHEMAS))}."
            ),
            operator_hint="Use a supported schema name or migrate to a new signed object.",
            retryable=False,
            details={"schema_name": schema_name, "schema_version": schema_version},
        )
    if schema_version not in supported_versions:
        raise BridgeError(
            code=SCHEMA_UNSUPPORTED,
            human_message=(
                f"Unsupported schema version v{schema_version} for '{schema_name}'. "
                f"Supported versions: {sorted(supported_versions)}."
            ),
            operator_hint="Create a new signed object with a supported schema version.",
            retryable=False,
            details={"schema_name": schema_name, "schema_version": schema_version},
        )


# --- Enums -----------------------------------------------------------------


class MeasurementMode(str, enum.Enum):
    """v1 measurement modes."""

    PHOTOMETRY = "photometry"
    FLUOROMETRY = "fluorometry"
    LUMINESCENCE = "luminescence"


class WellRole(str, enum.Enum):
    """Well intent in a plate layout.

    - ``measured``: included in MDB PlateMap, raw values collected.
    - ``skipped``: not included in MDB PlateMap, instrument skips it.
    - ``excluded``: measured (in PlateMap) but excluded from analysis calculations.
    """

    MEASURED = "measured"
    SKIPPED = "skipped"
    EXCLUDED = "excluded"


class ControlType(str, enum.Enum):
    """Control well classification for analysis normalization and blank subtraction."""

    BLANK = "blank"
    POSITIVE_CONTROL = "positive_control"
    NEGATIVE_CONTROL = "negative_control"


class ExecutionMode(str, enum.Enum):
    """Automation Job execution modes.

    - ``generated_protocol``: strict v1 authoring path requiring signed
      method.json, layout.json, analysis.json, and job.json.
    - ``existing_protocol``: legacy/advanced path for running pre-existing
      Wallac/OEM protocols by signed Automation Job reference.
    """

    GENERATED_PROTOCOL = "generated_protocol"
    EXISTING_PROTOCOL = "existing_protocol"


class LayoutSource(str, enum.Enum):
    """How the plate layout is provided to an Automation Job.

    - ``reusable``: links to a signed Wallac Victor2 Plate Layout resource.
    - ``one_off``: signed layout.json attachment on the Automation Job itself.
    """

    REUSABLE = "reusable"
    ONE_OFF = "one_off"


class LifecycleState(str, enum.Enum):
    """Shared lifecycle model for executable objects."""

    DRAFT = "draft"
    SIGNED_ACTIVE = "signed/active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    REVOKED = "revoked"


# Eligible lifecycle states for execution (plan: "Execution eligibility")
EXECUTABLE_LIFECYCLE_STATES: frozenset[str] = frozenset({LifecycleState.SIGNED_ACTIVE.value})


# --- 96-well plate helpers -------------------------------------------------

_PLATE_ROWS = "ABCDEFGH"
_PLATE_COLS = range(1, 13)

#: All 96 valid well names in canonical row-major order: A1, A2, ..., H12.
VALID_WELL_NAMES: tuple[str, ...] = tuple(
    f"{row}{col}" for row in _PLATE_ROWS for col in _PLATE_COLS
)

_VALID_WELL_NAMES_SET = frozenset(VALID_WELL_NAMES)


def is_valid_well_name(name: str) -> bool:
    """Return True if ``name`` is a valid 96-well plate well name (A1–H12)."""
    return name in _VALID_WELL_NAMES_SET


# --- Mode-specific settings ------------------------------------------------


@dataclass
class PhotometrySettings:
    """Absorbance/photometry acquisition settings.

    - One installed photometry filter per run (no arbitrary wavelengths).
    - Canonical execution uses physical Wallac filter identity (e.g. ``P610``).
    - UI aliases like ``OD600`` may be displayed but are not canonical.
    """

    filter_id: str
    filter_name: str
    read_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_id": self.filter_id,
            "filter_name": self.filter_name,
            "read_time_seconds": self.read_time_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PhotometrySettings:
        return cls(
            filter_id=str(d["filter_id"]),
            filter_name=str(d["filter_name"]),
            read_time_seconds=float(d["read_time_seconds"]),
        )


@dataclass
class FluorometrySettings:
    """Simple fluorometry acquisition settings.

    - One excitation filter, one emission filter, one read/integration setting.
    - No scans, ratios, dual labels, TRF timing, polarization, or correction.
    """

    excitation_filter_id: str
    excitation_filter_name: str
    emission_filter_id: str
    emission_filter_name: str
    read_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "excitation_filter_id": self.excitation_filter_id,
            "excitation_filter_name": self.excitation_filter_name,
            "emission_filter_id": self.emission_filter_id,
            "emission_filter_name": self.emission_filter_name,
            "read_time_seconds": self.read_time_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FluorometrySettings:
        return cls(
            excitation_filter_id=str(d["excitation_filter_id"]),
            excitation_filter_name=str(d["excitation_filter_name"]),
            emission_filter_id=str(d["emission_filter_id"]),
            emission_filter_name=str(d["emission_filter_name"]),
            read_time_seconds=float(d["read_time_seconds"]),
        )


@dataclass
class LuminescenceSettings:
    """Simple endpoint luminescence acquisition settings.

    - No excitation/emission filters.
    - One integration/counting setting.
    - No dispenser-triggered reads, kinetic loops, or delayed reads.
    """

    integration_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {"integration_time_seconds": self.integration_time_seconds}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LuminescenceSettings:
        return cls(integration_time_seconds=float(d["integration_time_seconds"]))


# --- Method schema (wallac.method.v1) -------------------------------------


@dataclass
class MethodSpec:
    """Canonical method specification (``wallac.method.v1``).

    Reusable acquisition settings.  Does NOT own the measured-well set
    (that belongs to the Plate Layout).

    Exactly one of ``photometry``, ``fluorometry``, or ``luminescence``
    must be set, matching ``mode``.
    """

    name: str
    mode: str  # MeasurementMode value
    plate_type: str = "96-well"
    photometry: PhotometrySettings | None = None
    fluorometry: FluorometrySettings | None = None
    luminescence: LuminescenceSettings | None = None

    @property
    def schema_name(self) -> str:
        return "wallac.method"

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION_V1

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "name": self.name,
            "plate_type": self.plate_type,
        }
        if self.photometry is not None:
            d["photometry"] = self.photometry.to_dict()
        if self.fluorometry is not None:
            d["fluorometry"] = self.fluorometry.to_dict()
        if self.luminescence is not None:
            d["luminescence"] = self.luminescence.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MethodSpec:
        schema_name = str(d.get("schema_name", ""))
        schema_version = int(d.get("schema_version", 0))
        validate_schema_identity(schema_name, schema_version)

        mode = str(d["mode"])
        if mode not in {m.value for m in MeasurementMode}:
            raise BridgeError(
                code=SCHEMA_UNSUPPORTED,
                human_message=f"Unsupported measurement mode '{mode}'.",
                details={"mode": mode},
            )

        photometry = None
        fluorometry = None
        luminescence = None

        if mode == MeasurementMode.PHOTOMETRY.value:
            if "photometry" not in d:
                raise ValueError("photometry settings required for mode='photometry'")
            photometry = PhotometrySettings.from_dict(d["photometry"])
        elif mode == MeasurementMode.FLUOROMETRY.value:
            if "fluorometry" not in d:
                raise ValueError("fluorometry settings required for mode='fluorometry'")
            fluorometry = FluorometrySettings.from_dict(d["fluorometry"])
        elif mode == MeasurementMode.LUMINESCENCE.value:
            if "luminescence" not in d:
                raise ValueError("luminescence settings required for mode='luminescence'")
            luminescence = LuminescenceSettings.from_dict(d["luminescence"])

        return cls(
            name=str(d["name"]),
            mode=mode,
            plate_type=str(d.get("plate_type", "96-well")),
            photometry=photometry,
            fluorometry=fluorometry,
            luminescence=luminescence,
        )


# --- Layout schema (wallac.layout.v1) -------------------------------------


@dataclass
class WellSpec:
    """A single well in a plate layout."""

    well_name: str
    role: str  # WellRole value
    sample_name: str = ""
    sample_label: str = ""
    replicate_group: str = ""
    control_type: str = ""  # ControlType value, empty if not a control
    elabftw_item_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "well_name": self.well_name,
            "role": self.role,
        }
        # Only include optional fields when non-default to keep canonical JSON compact.
        if self.sample_name:
            d["sample_name"] = self.sample_name
        if self.sample_label:
            d["sample_label"] = self.sample_label
        if self.replicate_group:
            d["replicate_group"] = self.replicate_group
        if self.control_type:
            d["control_type"] = self.control_type
        if self.elabftw_item_id:
            d["elabftw_item_id"] = self.elabftw_item_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WellSpec:
        well_name = str(d["well_name"])
        if not is_valid_well_name(well_name):
            raise ValueError(f"Invalid well name '{well_name}' (must be A1–H12)")

        role = str(d["role"])
        if role not in {r.value for r in WellRole}:
            raise ValueError(f"Invalid well role '{role}'")

        control_type = str(d.get("control_type", ""))
        if control_type and control_type not in {c.value for c in ControlType}:
            raise ValueError(f"Invalid control_type '{control_type}'")

        return cls(
            well_name=well_name,
            role=role,
            sample_name=str(d.get("sample_name", "")),
            sample_label=str(d.get("sample_label", "")),
            replicate_group=str(d.get("replicate_group", "")),
            control_type=control_type,
            elabftw_item_id=int(d.get("elabftw_item_id", 0)),
        )


@dataclass
class LayoutSpec:
    """Canonical plate layout specification (``wallac.layout.v1``).

    Well/sample map and measured/skipped/excluded well intent.
    v1 supports only the 96-well plate type.
    """

    plate_type: str = "96-well"
    wells: list[WellSpec] = field(default_factory=list)

    @property
    def schema_name(self) -> str:
        return "wallac.layout"

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION_V1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "plate_type": self.plate_type,
            "wells": [w.to_dict() for w in self.wells],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LayoutSpec:
        schema_name = str(d.get("schema_name", ""))
        schema_version = int(d.get("schema_version", 0))
        validate_schema_identity(schema_name, schema_version)

        wells = [WellSpec.from_dict(w) for w in d["wells"]]
        return cls(
            plate_type=str(d.get("plate_type", "96-well")),
            wells=wells,
        )


# --- Analysis schema (wallac.analysis.v1) ----------------------------------


@dataclass
class BlankSubtractionConfig:
    """Blank subtraction configuration."""

    enabled: bool = False
    blank_wells: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "blank_wells": list(self.blank_wells),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BlankSubtractionConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            blank_wells=[str(w) for w in d.get("blank_wells", [])],
        )


@dataclass
class ReplicateAggregationConfig:
    """Replicate aggregation configuration: mean, SD, CV, N."""

    enabled: bool = False
    group_by: str = "replicate_group"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "group_by": self.group_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplicateAggregationConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            group_by=str(d.get("group_by", "replicate_group")),
        )


@dataclass
class NormalizationConfig:
    """Normalization to control wells/groups."""

    enabled: bool = False
    control_type: str = ""  # ControlType value
    target_value: float = 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "control_type": self.control_type,
            "target_value": self.target_value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NormalizationConfig:
        ct = str(d.get("control_type", ""))
        if ct and ct not in {c.value for c in ControlType}:
            raise ValueError(f"Invalid control_type '{ct}'")
        return cls(
            enabled=bool(d.get("enabled", False)),
            control_type=ct,
            target_value=float(d.get("target_value", 100.0)),
        )


@dataclass
class ThresholdRule:
    """A single pass/fail threshold rule."""

    name: str
    metric: str  # e.g. "primary_value", "cv"
    operator: str  # ">=", "<=", ">", "<", "==", "!="
    value: float
    action: str = "flag"  # "flag" or "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "operator": self.operator,
            "value": self.value,
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ThresholdRule:
        return cls(
            name=str(d["name"]),
            metric=str(d["metric"]),
            operator=str(d["operator"]),
            value=float(d["value"]),
            action=str(d.get("action", "flag")),
        )


@dataclass
class AnalysisSpec:
    """Canonical analysis plan specification (``wallac.analysis.v1``).

    Defines the fixed v1 analysis pipeline order:
    load raw → mark skipped → apply exclusions → compute blank →
    subtract blank → compute normalization factor → apply normalization →
    aggregate replicates → apply thresholds → emit artifacts.
    """

    blank_subtraction: BlankSubtractionConfig = field(default_factory=BlankSubtractionConfig)
    replicate_aggregation: ReplicateAggregationConfig = field(
        default_factory=ReplicateAggregationConfig
    )
    normalization: NormalizationConfig = field(default_factory=NormalizationConfig)
    thresholds: list[ThresholdRule] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    outputs: list[str] = field(
        default_factory=lambda: [
            "raw_results",
            "analyzed_wells",
            "replicate_summary",
            "analysis_summary",
        ]
    )

    @property
    def schema_name(self) -> str:
        return "wallac.analysis"

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION_V1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "blank_subtraction": self.blank_subtraction.to_dict(),
            "replicate_aggregation": self.replicate_aggregation.to_dict(),
            "normalization": self.normalization.to_dict(),
            "thresholds": [t.to_dict() for t in self.thresholds],
            "exclusions": list(self.exclusions),
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisSpec:
        schema_name = str(d.get("schema_name", ""))
        schema_version = int(d.get("schema_version", 0))
        validate_schema_identity(schema_name, schema_version)

        return cls(
            blank_subtraction=BlankSubtractionConfig.from_dict(d.get("blank_subtraction", {})),
            replicate_aggregation=ReplicateAggregationConfig.from_dict(
                d.get("replicate_aggregation", {})
            ),
            normalization=NormalizationConfig.from_dict(d.get("normalization", {})),
            thresholds=[ThresholdRule.from_dict(t) for t in d.get("thresholds", [])],
            exclusions=[str(w) for w in d.get("exclusions", [])],
            outputs=[str(o) for o in d.get("outputs", [])],
        )


# --- Job schema (wallac.job.v1) -------------------------------------------


@dataclass
class ObjectReference:
    """Reference to a signed eLabFTW object by ID, hash, and attachment ID.

    Automation Jobs bind to specific signed object versions by ID and hash.
    They must never resolve ``latest active`` at execution time.
    """

    object_id: int
    hash: str  # SHA-256 hex of the canonical JSON
    json_attachment_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "hash": self.hash,
            "json_attachment_id": self.json_attachment_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObjectReference:
        return cls(
            object_id=int(d["object_id"]),
            hash=str(d["hash"]),
            json_attachment_id=int(d["json_attachment_id"]),
        )


@dataclass
class LayoutReference:
    """Reference to a plate layout, either reusable or one-off.

    - ``reusable``: links to a signed Wallac Victor2 Plate Layout resource
      (``object_id`` + ``hash`` + ``json_attachment_id``).
    - ``one_off``: signed layout.json attachment on the Automation Job itself
      (``hash`` + ``json_attachment_id``, no ``object_id``).
    """

    source: str  # LayoutSource value
    hash: str
    json_attachment_id: int
    object_id: int = 0  # only for reusable

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "source": self.source,
            "hash": self.hash,
            "json_attachment_id": self.json_attachment_id,
        }
        if self.source == LayoutSource.REUSABLE.value:
            d["object_id"] = self.object_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LayoutReference:
        source = str(d["source"])
        if source not in {s.value for s in LayoutSource}:
            raise ValueError(f"Invalid layout source '{source}'")

        obj = cls(
            source=source,
            hash=str(d["hash"]),
            json_attachment_id=int(d["json_attachment_id"]),
            object_id=int(d.get("object_id", 0)),
        )
        if source == LayoutSource.REUSABLE.value and not obj.object_id:
            raise ValueError("reusable layout reference requires object_id")
        return obj


@dataclass
class JobSpec:
    """Canonical automation job specification (``wallac.job.v1``).

    The final frozen execution bundle.  For ``generated_protocol`` mode,
    requires signed Method, Layout, and Analysis references.  For
    ``existing_protocol`` mode, requires a protocol name or AssayProtID.
    """

    execution_mode: str  # ExecutionMode value
    method: ObjectReference | None = None
    layout: LayoutReference | None = None
    analysis: ObjectReference | None = None
    protocol_name: str = ""  # only for existing_protocol
    assay_prot_id: int = 0  # only for existing_protocol

    @property
    def schema_name(self) -> str:
        return "wallac.job"

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION_V1

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "execution_mode": self.execution_mode,
        }
        if self.execution_mode == ExecutionMode.GENERATED_PROTOCOL.value:
            if self.method is not None:
                d["method"] = self.method.to_dict()
            if self.layout is not None:
                d["layout"] = self.layout.to_dict()
            if self.analysis is not None:
                d["analysis"] = self.analysis.to_dict()
        elif self.execution_mode == ExecutionMode.EXISTING_PROTOCOL.value:
            if self.protocol_name:
                d["protocol_name"] = self.protocol_name
            if self.assay_prot_id:
                d["assay_prot_id"] = self.assay_prot_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobSpec:
        schema_name = str(d.get("schema_name", ""))
        schema_version = int(d.get("schema_version", 0))
        validate_schema_identity(schema_name, schema_version)

        execution_mode = str(d["execution_mode"])
        if execution_mode not in {m.value for m in ExecutionMode}:
            raise BridgeError(
                code=SCHEMA_UNSUPPORTED,
                human_message=f"Unsupported execution mode '{execution_mode}'.",
                details={"execution_mode": execution_mode},
            )

        method = None
        layout = None
        analysis = None
        protocol_name = ""
        assay_prot_id = 0

        if execution_mode == ExecutionMode.GENERATED_PROTOCOL.value:
            if "method" in d:
                method = ObjectReference.from_dict(d["method"])
            if "layout" in d:
                layout = LayoutReference.from_dict(d["layout"])
            if "analysis" in d:
                analysis = ObjectReference.from_dict(d["analysis"])
        elif execution_mode == ExecutionMode.EXISTING_PROTOCOL.value:
            protocol_name = str(d.get("protocol_name", ""))
            assay_prot_id = int(d.get("assay_prot_id", 0))

        return cls(
            execution_mode=execution_mode,
            method=method,
            layout=layout,
            analysis=analysis,
            protocol_name=protocol_name,
            assay_prot_id=assay_prot_id,
        )
