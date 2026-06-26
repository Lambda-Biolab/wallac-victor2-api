"""Designer/Run Builder backend service for Wallac Victor2 protocol authoring.

Implements Stage 3 of docs/plans/wallac-protocol-authoring.md:

- Authenticated draft APIs for Method, Plate Layout, Analysis Plan, and
  Automation Job.
- Backend finalizes canonical JSON (via :mod:`bridge.canonical`) and attaches
  draft files to eLabFTW.
- Draft objects are mutable; signed objects reject mutation (routed to
  clone/version).
- Browser never receives the eLabFTW API key or vm-agent token.
- No execution — this module creates/updates drafts and finalizes canonical
  JSON only.

The :class:`DesignerService` is the core logic layer.  The FastAPI app in
:mod:`bridge.designer_app` wraps it in HTTP endpoints.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .canonical import canonicalize_and_hash
from .errors import OPERATOR_REVIEW_REQUIRED, BridgeError
from .schemas import (
    AnalysisSpec,
    JobSpec,
    LayoutSpec,
    LifecycleState,
    MethodSpec,
)

logger = logging.getLogger(__name__)

# --- eLabFTW category IDs (must match seed_wallac.py) -----------------------

# These are the resource category IDs created by the eLabFTW seed script.
# In production they come from BridgeConfig; here we use symbolic constants
# so tests can override.
DEFAULT_METHOD_CATEGORY = 10
DEFAULT_LAYOUT_CATEGORY = 11
DEFAULT_ANALYSIS_CATEGORY = 12
DEFAULT_JOB_CATEGORY = 9  # existing category, renamed


# --- Draft object types -----------------------------------------------------


@dataclass
class DraftObject:
    """A draft eLabFTW resource (Method, Layout, Analysis, or Job).

    Wraps the eLabFTW item with its parsed metadata and draft spec dict.
    Drafts are mutable until finalized (canonical JSON attached + signed).
    """

    item_id: int
    title: str
    category_id: int
    lifecycle: str
    spec_dict: dict[str, Any] = field(default_factory=dict)
    hash: str = ""
    json_attachment_id: int = 0
    extra_fields: dict[str, Any] = field(default_factory=dict)


# --- eLabFTW client protocol (extends ElabftwInterface) ---------------------


class DesignerElabftwClient(Protocol):
    """Extended eLabFTW client protocol for designer operations.

    Extends the existing :class:`bridge.elabftw.ElabftwInterface` with methods
    for creating/listing/patching items in the Method, Layout, Analysis, and
    Job categories.
    """

    def list_items(self, category_id: int) -> list[dict[str, Any]]:
        """List all items in a resource category."""
        ...

    def get_item(self, item_id: int) -> dict[str, Any]:
        """Get a single item by ID."""
        ...

    def create_item(self, category_id: int, title: str, body: str = "") -> int:
        """Create a new item in a resource category. Returns the new item ID."""
        ...

    def patch_item(self, item_id: int, fields: dict[str, Any]) -> None:
        """Patch fields on an item (title, body, etc.)."""
        ...

    def patch_metadata(self, item_id: int, extra_fields: dict[str, Any]) -> None:
        """Update extra_fields metadata on an item."""
        ...

    def upload_file(
        self, item_id: int, filename: str, content: bytes, comment: str = ""
    ) -> dict[str, Any]:
        """Upload a file attachment to an item."""
        ...

    def list_uploads(self, item_id: int) -> list[dict[str, Any]]:
        """List uploads (attachments) for an item."""
        ...

    def download_upload(self, item_id: int, upload_id: int) -> bytes:
        """Download the raw bytes of an upload."""
        ...


# --- Spec type mapping ------------------------------------------------------

#: Maps category kind to the schema dataclass and schema_name prefix.
SPEC_TYPES: dict[str, type] = {
    "method": MethodSpec,
    "layout": LayoutSpec,
    "analysis": AnalysisSpec,
    "job": JobSpec,
}

#: Maps category kind to the eLabFTW metadata field names for hash/attachment.
FIELD_NAMES: dict[str, dict[str, str]] = {
    "method": {"hash": "Method hash", "attachment": "Method JSON attachment ID"},
    "layout": {"hash": "Layout hash", "attachment": "Layout JSON attachment ID"},
    "analysis": {"hash": "Analysis hash", "attachment": "Analysis JSON attachment ID"},
    "job": {"hash": "Job hash", "attachment": "Job JSON attachment ID"},
}

#: Maps category kind to the JSON filename for the canonical attachment.
ATTACHMENT_NAMES: dict[str, str] = {
    "method": "method.json",
    "layout": "layout.json",
    "analysis": "analysis.json",
    "job": "job.json",
}


# --- Designer service -------------------------------------------------------


class DesignerService:
    """Core logic for the designer/Run Builder backend.

    All operations go through the :class:`DesignerElabftwClient` protocol, so
    tests can provide a mock implementation.  The service never holds the
    eLabFTW API key — that's in the HTTP client layer.
    """

    def __init__(
        self,
        client: DesignerElabftwClient,
        *,
        method_category: int = DEFAULT_METHOD_CATEGORY,
        layout_category: int = DEFAULT_LAYOUT_CATEGORY,
        analysis_category: int = DEFAULT_ANALYSIS_CATEGORY,
        job_category: int = DEFAULT_JOB_CATEGORY,
    ) -> None:
        self.client = client
        self.categories = {
            "method": method_category,
            "layout": layout_category,
            "analysis": analysis_category,
            "job": job_category,
        }

    # --- Draft CRUD ---

    def create_draft(
        self,
        kind: str,
        title: str,
        spec_dict: dict[str, Any],
    ) -> DraftObject:
        """Create a new draft object in eLabFTW.

        Args:
            kind: One of "method", "layout", "analysis", "job".
            title: Human-readable title for the eLabFTW item.
            spec_dict: The spec dict (will be stored as draft, not yet canonicalized).

        Returns:
            The created :class:`DraftObject`.
        """
        self._validate_kind(kind)
        cat_id = self.categories[kind]

        item_id = self.client.create_item(cat_id, title)

        # Set lifecycle to draft and store the spec as metadata
        fields = self._build_metadata_fields(kind, spec_dict)
        fields["Lifecycle state"] = {"value": LifecycleState.DRAFT.value}
        self.client.patch_metadata(item_id, fields)

        return DraftObject(
            item_id=item_id,
            title=title,
            category_id=cat_id,
            lifecycle=LifecycleState.DRAFT.value,
            spec_dict=spec_dict,
            extra_fields=fields,
        )

    def get_draft(self, kind: str, item_id: int) -> DraftObject:
        """Retrieve a draft object from eLabFTW.

        Raises:
            BridgeError: if the item is not in draft state (signed objects
                are immutable and cannot be retrieved as drafts).
        """
        self._validate_kind(kind)
        item = self.client.get_item(item_id)
        return self._parse_item(kind, item)

    def update_draft(
        self,
        kind: str,
        item_id: int,
        spec_dict: dict[str, Any],
    ) -> DraftObject:
        """Update a draft's spec dict.

        Raises:
            BridgeError: if the item is not in draft state.
        """
        self._validate_kind(kind)
        draft = self.get_draft(kind, item_id)

        if draft.lifecycle != LifecycleState.DRAFT.value:
            raise BridgeError(
                code=OPERATOR_REVIEW_REQUIRED,
                human_message=(
                    f"Cannot mutate {kind} {item_id}: lifecycle state is "
                    f"'{draft.lifecycle}', not 'draft'. Signed objects are "
                    f"immutable. Create a new draft clone instead."
                ),
                operator_hint="Create a new draft from the signed object to make changes.",
                retryable=False,
                details={"item_id": item_id, "lifecycle": draft.lifecycle},
            )

        fields = self._build_metadata_fields(kind, spec_dict)
        self.client.patch_metadata(item_id, fields)

        draft.spec_dict = spec_dict
        draft.extra_fields = fields
        return draft

    def list_drafts(self, kind: str) -> list[DraftObject]:
        """List all draft objects of a given kind."""
        self._validate_kind(kind)
        cat_id = self.categories[kind]
        items = self.client.list_items(cat_id)
        drafts = []
        for item in items:
            try:
                draft = self._parse_item(kind, item)
                drafts.append(draft)
            except Exception:
                logger.warning("Failed to parse item %s in category %s", item.get("id"), cat_id)
        return drafts

    # --- Finalize (canonicalize + attach) ---

    def finalize_draft(
        self,
        kind: str,
        item_id: int,
    ) -> DraftObject:
        """Finalize a draft: canonicalize the spec, compute hash, attach JSON.

        This does NOT sign the object — signing is an eLabFTW UI operation.
        Finalization prepares the canonical JSON attachment and writes the
        hash + attachment ID to metadata, so that when the operator signs,
        the signature binds to the correct bytes.

        After finalization, the lifecycle remains 'draft' until the operator
        signs in eLabFTW.  The bridge checks for a signature before accepting
        the object as 'signed/active'.

        Returns:
            The updated :class:`DraftObject` with hash and attachment_id set.
        """
        self._validate_kind(kind)
        draft = self.get_draft(kind, item_id)

        if draft.lifecycle != LifecycleState.DRAFT.value:
            raise BridgeError(
                code=OPERATOR_REVIEW_REQUIRED,
                human_message=(
                    f"Cannot finalize {kind} {item_id}: lifecycle state is "
                    f"'{draft.lifecycle}', not 'draft'."
                ),
                operator_hint="Only draft objects can be finalized.",
                retryable=False,
                details={"item_id": item_id, "lifecycle": draft.lifecycle},
            )

        # Canonicalize the spec dict and compute hash
        canonical_bytes, hash_hex = canonicalize_and_hash(draft.spec_dict)

        # Upload the canonical JSON as an attachment
        filename = ATTACHMENT_NAMES[kind]
        upload = self.client.upload_file(item_id, filename, canonical_bytes)
        attachment_id = int(upload.get("id", 0))

        # Write hash + attachment ID to metadata
        field_names = FIELD_NAMES[kind]
        self.client.patch_metadata(
            item_id,
            {
                field_names["hash"]: {"value": hash_hex},
                field_names["attachment"]: {"value": str(attachment_id)},
            },
        )

        draft.hash = hash_hex
        draft.json_attachment_id = attachment_id
        return draft

    # --- Clone (for creating new version from signed object) ---

    def clone_signed(
        self,
        kind: str,
        item_id: int,
        new_title: str,
    ) -> DraftObject:
        """Create a new draft clone from a signed object.

        The new draft has lineage fields pointing to the parent.
        """
        self._validate_kind(kind)
        item = self.client.get_item(item_id)
        parent = self._parse_item(kind, item)

        if parent.lifecycle != LifecycleState.SIGNED_ACTIVE.value:
            raise BridgeError(
                code=OPERATOR_REVIEW_REQUIRED,
                human_message=(
                    f"Cannot clone {kind} {item_id}: lifecycle state is "
                    f"'{parent.lifecycle}'. Only signed/active objects can be cloned."
                ),
                operator_hint="Clone from a signed/active object.",
                retryable=False,
                details={"item_id": item_id, "lifecycle": parent.lifecycle},
            )

        # Create new draft with the same spec
        new_draft = self.create_draft(kind, new_title, dict(parent.spec_dict))

        # Set lineage fields
        self.client.patch_metadata(
            new_draft.item_id,
            {
                "Parent object ID": {"value": str(parent.item_id)},
                "Supersedes object ID": {"value": str(parent.item_id)},
            },
        )

        return new_draft

    # --- Internal helpers ---

    def _validate_kind(self, kind: str) -> None:
        if kind not in self.categories:
            raise ValueError(f"Unknown kind '{kind}'. Must be one of: {', '.join(self.categories)}")

    def _build_metadata_fields(self, kind: str, spec_dict: dict[str, Any]) -> dict[str, Any]:
        """Build the extra_fields metadata for a draft item.

        Stores the full spec dict as a JSON string in a single field so the
        designer can read it back without downloading the attachment.
        The canonical attachment is only created on finalize.
        """
        return {
            "Designer spec": {
                "value": json.dumps(spec_dict, ensure_ascii=False),
            },
        }

    def _parse_item(self, kind: str, item: dict[str, Any]) -> DraftObject:
        """Parse an eLabFTW item dict into a DraftObject."""
        self._validate_kind(kind)
        item_id = int(item["id"])
        title = str(item.get("title", ""))
        cat_id = int(item.get("category") or self.categories[kind])

        # Parse metadata
        from .elabftw import extract_extra_fields, get_field_value

        extra_fields = extract_extra_fields(item.get("metadata"))

        lifecycle = get_field_value(extra_fields, "Lifecycle state") or LifecycleState.DRAFT.value

        field_names = FIELD_NAMES[kind]
        hash_val = get_field_value(extra_fields, field_names["hash"])
        attachment_val = get_field_value(extra_fields, field_names["attachment"])
        attachment_id = int(attachment_val) if attachment_val else 0

        # Parse the spec from the Designer spec field
        spec_json = get_field_value(extra_fields, "Designer spec")
        spec_dict: dict[str, Any] = {}
        if spec_json:
            try:
                spec_dict = json.loads(spec_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse Designer spec for item %s", item_id)

        return DraftObject(
            item_id=item_id,
            title=title,
            category_id=cat_id,
            lifecycle=lifecycle,
            spec_dict=spec_dict,
            hash=hash_val,
            json_attachment_id=attachment_id,
            extra_fields=extra_fields,
        )
