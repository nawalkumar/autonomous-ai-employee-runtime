"""Build, select and expand bounded collection workflows."""

from __future__ import annotations

import copy
from typing import Any

from app.models.collections import (
    DEFAULT_COLLECTION_LIMIT,
    MAX_COLLECTION_ITEMS,
    REGISTERED_COLLECTION_SOURCES,
    CollectionItem,
    CollectionProgress,
    CollectionSelect,
    CollectionSnapshot,
    CollectionWork,
    parse_collection_item_ref,
)
from app.models.enums import ToolCallStatus
from app.models.ids import new_id
from app.models.plan import PlanStep
from app.models.references import BindingError, read_path
from app.models.state import ExecutionState
from app.tools.capabilities import operation_of


class CollectionError(ValueError):
    """Fail-closed collection observation or expansion error."""


def build_ticket_collection_snapshot(
    state: ExecutionState,
    *,
    source_step_id: str,
    max_items: int = DEFAULT_COLLECTION_LIMIT,
    item_id_field: str = "ticket_id",
    items_path: list[str | int] | None = None,
) -> CollectionSnapshot:
    """Seal a durable ticket collection from a successful list_tickets call."""
    if max_items < 1 or max_items > MAX_COLLECTION_ITEMS:
        raise CollectionError(f"max_items must be 1-{MAX_COLLECTION_ITEMS}")
    path = list(items_path or ["tickets"])
    call = _successful_call_for_step(state, source_step_id)
    tool = call.tool_name
    operation = operation_of(call.arguments or {})
    if not isinstance(operation, str) or (tool, operation) not in REGISTERED_COLLECTION_SOURCES:
        raise CollectionError(
            f"Step '{source_step_id}' is not a registered collection source "
            f"({tool}.{operation})."
        )
    data = (call.result or {}).get("data")
    if not isinstance(data, dict):
        raise CollectionError(f"Step '{source_step_id}' has no result data")
    try:
        raw_items = read_path(data, tuple(path), f"step '{source_step_id}' collection")
    except BindingError as exc:
        raise CollectionError(str(exc)) from exc
    if not isinstance(raw_items, list):
        raise CollectionError("Collection path must resolve to a list")

    items: list[CollectionItem] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    truncated = False
    for index, raw in enumerate(raw_items):
        if len(items) >= max_items:
            truncated = True
            break
        if not isinstance(raw, dict):
            raise CollectionError(f"Collection item at index {index} is not an object")
        item_id = raw.get(item_id_field)
        if not isinstance(item_id, str) or not item_id:
            raise CollectionError(
                f"Collection item at index {index} missing {item_id_field!r}"
            )
        if item_id in seen:
            duplicates.append(item_id)
            continue
        seen.add(item_id)
        # Shallow copy of scalar fields only.
        fields = {
            key: value
            for key, value in raw.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        items.append(
            CollectionItem(
                index=len(items),
                item_id=item_id,
                fields=fields,
                source_step_id=source_step_id,
                source_call_id=call.call_id,
                source_path=[*path, index],
            )
        )

    return CollectionSnapshot(
        collection_id=new_id("coll_"),
        source_tool=tool,
        source_operation=operation,
        source_step_id=source_step_id,
        source_call_id=call.call_id,
        item_id_field=item_id_field,
        items=items,
        max_items=max_items,
        truncated=truncated,
        duplicate_ids_removed=duplicates,
    )


def select_collection_items(
    snapshot: CollectionSnapshot,
    rule: CollectionSelect,
    *,
    max_items: int | None = None,
) -> list[CollectionItem]:
    """Apply a deterministic selection rule; never exceeds snapshot or max_items."""
    ceiling = min(snapshot.max_items, max_items or snapshot.max_items, MAX_COLLECTION_ITEMS)
    if rule.mode == "first_n":
        assert rule.n is not None
        return list(snapshot.items[: min(rule.n, ceiling)])
    assert rule.ids is not None
    by_id = {item.item_id: item for item in snapshot.items}
    selected: list[CollectionItem] = []
    for item_id in rule.ids:
        if len(selected) >= ceiling:
            break
        item = by_id.get(item_id)
        if item is None:
            raise CollectionError(
                f"Selected item {item_id!r} is not in the sealed collection "
                f"from step '{snapshot.source_step_id}'."
            )
        selected.append(item)
    return selected


def expand_collection_step(
    template: PlanStep,
    work: CollectionWork,
    selected: list[CollectionItem],
) -> list[PlanStep]:
    """Expand one collection template step into ordinary concrete steps."""
    if len(selected) > work.max_items:
        raise CollectionError("Selection exceeds collection max_items")
    expanded: list[PlanStep] = []
    for item in selected:
        step_id = f"{template.step_id}__{item.item_id}"
        arguments = _bind_collection_placeholders(
            copy.deepcopy(template.arguments or {}), item
        )
        expanded.append(
            template.model_copy(
                update={
                    "step_id": step_id,
                    "arguments": arguments,
                    "collection": None,
                    "description": f"{template.description} [{item.item_id}]",
                }
            )
        )
    return expanded


def _bind_collection_placeholders(value: Any, item: CollectionItem) -> Any:
    try:
        ref = parse_collection_item_ref(value)
    except ValueError as exc:
        raise CollectionError(str(exc)) from exc
    if ref is not None:
        if ref.field == "item_id" or ref.field == item.source_step_id:
            return item.item_id
        if ref.field in item.fields:
            return item.fields[ref.field]
        if ref.field == "ticket_id":
            return item.item_id
        raise CollectionError(f"Collection item has no field {ref.field!r}")
    if isinstance(value, dict):
        return {key: _bind_collection_placeholders(val, item) for key, val in value.items()}
    if isinstance(value, list):
        return [_bind_collection_placeholders(val, item) for val in value]
    return value


def _successful_call_for_step(state: ExecutionState, step_id: str):
    for call in reversed(list(state.tool_calls or [])):
        if call.step_id == step_id and call.status is ToolCallStatus.SUCCEEDED:
            return call
    raise CollectionError(f"No successful tool call for collection source '{step_id}'")


def progress_records(
    collection_id: str,
    expanded: list[PlanStep],
    selected: list[CollectionItem],
) -> list[CollectionProgress]:
    records: list[CollectionProgress] = []
    for step, item in zip(expanded, selected, strict=True):
        records.append(
            CollectionProgress(
                collection_id=collection_id,
                item_id=item.item_id,
                step_id=step.step_id,
                status="pending",
            )
        )
    return records
