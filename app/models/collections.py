"""Typed, bounded collection observations (Milestone 6).

Collections are durable snapshots of registered list outputs. They never grant
authority by themselves — mutations still require per-item authorization against
the task scope. No JSONPath, code evaluation or unbounded iteration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal  # Any used by parse_collection_item_ref

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Hard ceiling for any collection snapshot or expansion.
MAX_COLLECTION_ITEMS = 50
DEFAULT_COLLECTION_LIMIT = 20

COLLECTION_REF_KEY = "$collection_item"

# Only these tool.operation pairs may produce collection snapshots.
REGISTERED_COLLECTION_SOURCES: frozenset[tuple[str, str]] = frozenset(
    {("company_api", "list_tickets")}
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CollectionItem(BaseModel):
    """One ordered item from an authorized collection observation."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=MAX_COLLECTION_ITEMS - 1)
    item_id: str = Field(min_length=1, max_length=64)
    # Shallow, typed fields copied from the producer output (no nested blobs).
    fields: dict[str, Any] = Field(default_factory=dict)
    # Provenance: producer step and path within its successful tool data.
    source_step_id: str = Field(min_length=1, max_length=128)
    source_call_id: str | None = None
    source_path: list[str | int] = Field(min_length=1, max_length=8)


class CollectionSnapshot(BaseModel):
    """Durable, ordered collection sealed after a successful list operation."""

    model_config = ConfigDict(extra="forbid")

    collection_id: str = Field(min_length=1, max_length=128)
    source_tool: str
    source_operation: str
    source_step_id: str
    source_call_id: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    item_id_field: str = "ticket_id"
    items: list[CollectionItem] = Field(default_factory=list)
    max_items: int = Field(default=DEFAULT_COLLECTION_LIMIT, ge=1, le=MAX_COLLECTION_ITEMS)
    truncated: bool = False
    empty: bool = False
    duplicate_ids_removed: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounds(self) -> CollectionSnapshot:
        if (self.source_tool, self.source_operation) not in REGISTERED_COLLECTION_SOURCES:
            raise ValueError(
                f"Unregistered collection source {self.source_tool}.{self.source_operation}"
            )
        if len(self.items) > self.max_items:
            raise ValueError("Collection items exceed max_items")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Collection items must have unique item_id values")
        self.empty = len(self.items) == 0
        return self

    def item_ids(self) -> list[str]:
        return [item.item_id for item in self.items]


class CollectionSelect(BaseModel):
    """Deterministic selection over a sealed collection snapshot."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["first_n", "explicit_ids"]
    n: int | None = Field(default=None, ge=1, le=MAX_COLLECTION_ITEMS)
    ids: list[str] | None = None

    @model_validator(mode="after")
    def _mode_args(self) -> CollectionSelect:
        if self.mode == "first_n":
            if self.n is None:
                raise ValueError("first_n selection requires n")
        elif self.mode == "explicit_ids":
            if not self.ids:
                raise ValueError("explicit_ids selection requires ids")
            if len(self.ids) != len(set(self.ids)):
                raise ValueError("explicit_ids must be unique")
        return self


class CollectionWork(BaseModel):
    """Declarative per-step collection expansion attached to a PlanStep."""

    model_config = ConfigDict(extra="forbid")

    from_step: str = Field(min_length=1, max_length=128)
    items_path: list[str | int] = Field(default_factory=lambda: ["tickets"])
    item_id_field: str = "ticket_id"
    select: CollectionSelect
    max_items: int = Field(default=DEFAULT_COLLECTION_LIMIT, ge=1, le=MAX_COLLECTION_ITEMS)

    @field_validator("items_path")
    @classmethod
    def _path(cls, path: list[str | int]) -> list[str | int]:
        if not 1 <= len(path) <= 8:
            raise ValueError("items_path must have 1-8 segments")
        return path


class CollectionItemRef(BaseModel):
    """Placeholder in step arguments replaced during collection expansion."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=64)


def parse_collection_item_ref(value: Any) -> CollectionItemRef | None:
    """Return a collection-item placeholder, or None if ``value`` is unrelated.

    Raises ValueError for ambiguous ``$collection_item`` objects.
    """
    if not isinstance(value, dict) or COLLECTION_REF_KEY not in value:
        return None
    if set(value) != {COLLECTION_REF_KEY}:
        raise ValueError("Ambiguous $collection_item object")
    inner = value[COLLECTION_REF_KEY]
    if not isinstance(inner, dict):
        raise ValueError("$collection_item value must be an object")
    return CollectionItemRef.model_validate(inner)


# Derived from plan-step, approval and verification state. "verified" is set only
# after independent verification passes — a completed step or model reply is not
# success. "succeeded" is accepted only as a legacy alias for "completed".
CollectionItemStatus = Literal[
    "pending",
    "running",
    "awaiting_approval",
    "completed",
    "verified",
    "failed",
    "rejected",
    "skipped",
]

_LEGACY_PROGRESS_STATUS = {"succeeded": "completed"}


class CollectionProgress(BaseModel):
    """Per-item progress for an expanded collection workflow.

    ``status`` is a projection of the linked plan step plus task approval and
    verification. It is not a second record of what the model claimed.
    """

    model_config = ConfigDict(extra="forbid")

    collection_id: str
    item_id: str
    step_id: str
    status: CollectionItemStatus = "pending"
    error: str | None = Field(default=None, max_length=200)

    @field_validator("status", mode="before")
    @classmethod
    def _legacy_status(cls, value: object) -> object:
        if isinstance(value, str):
            return _LEGACY_PROGRESS_STATUS.get(value, value)
        return value


class CollectionExecutionState(BaseModel):
    """Task-level collection bookkeeping persisted with ExecutionState."""

    model_config = ConfigDict(extra="forbid")

    snapshots: dict[str, CollectionSnapshot] = Field(default_factory=dict)
    progress: list[CollectionProgress] = Field(default_factory=list)
    expanded_from: dict[str, str] = Field(default_factory=dict)  # new_step_id -> template_id
