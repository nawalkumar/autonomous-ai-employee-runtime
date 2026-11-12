"""Refuse ordinary saves that would install an unaudited plan revision.

Execution may persist status updates, tool-call history and a collection
expansion. A change to the replan counters, the authorization fingerprint, or
the step-id sequence is a revision and has to go through ``commit_replan``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models.enums import PlanStepStatus, ToolCallStatus
from app.models.plan import PlanStep
from app.models.state import ExecutionState
from app.tools.capabilities import operation_of


class RevisionPersistenceError(RuntimeError):
    """An ordinary save tried to persist a plan revision."""


class StaleRevisionError(RuntimeError):
    """The committed task changed before this revision could be installed."""


def plan_structure_token(steps: list[PlanStep]) -> str:
    """Identity of step ids, tools, operations, arguments and collection shape."""
    payload = [_step_payload(step) for step in steps]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def assert_ordinary_save(stored: ExecutionState, incoming: ExecutionState) -> None:
    """Raise when ``incoming`` would publish a revision the commit path owns."""
    if incoming.replan_count != stored.replan_count:
        raise RevisionPersistenceError(
            "Ordinary save cannot change replan_count; use commit_replan."
        )
    if incoming.replan_limit != stored.replan_limit:
        raise RevisionPersistenceError(
            "Ordinary save cannot change replan_limit; use commit_replan."
        )
    if (incoming.last_replan_trigger or None) != (stored.last_replan_trigger or None):
        raise RevisionPersistenceError(
            "Ordinary save cannot record a replan trigger; use commit_replan."
        )
    if incoming.authorization_fingerprint != stored.authorization_fingerprint:
        raise RevisionPersistenceError(
            "Ordinary save cannot change the authorization fingerprint."
        )
    if stored.authorization is not None and (
        incoming.authorization is None
        or incoming.authorization.fingerprint() != stored.authorization.fingerprint()
    ):
        raise RevisionPersistenceError(
            "Ordinary save cannot replace the task authorization scope."
        )
    _assert_history_kept(stored, incoming)
    if _step_ids(stored.plan) == _step_ids(incoming.plan):
        return
    if _is_collection_expansion(stored, incoming):
        return
    raise RevisionPersistenceError(
        "Ordinary save cannot replace plan steps; use commit_replan."
    )


def _assert_history_kept(stored: ExecutionState, incoming: ExecutionState) -> None:
    if not set(stored.completed_steps).issubset(set(incoming.completed_steps)):
        raise RevisionPersistenceError(
            "Ordinary save cannot drop completed steps."
        )
    incoming_calls = {call.call_id: call for call in incoming.tool_calls or []}
    for call in stored.tool_calls or []:
        kept = incoming_calls.get(call.call_id)
        if kept is None:
            raise RevisionPersistenceError(
                "Ordinary save cannot drop historical tool calls."
            )
        if (
            call.status is ToolCallStatus.SUCCEEDED
            and kept.status is not ToolCallStatus.SUCCEEDED
        ):
            raise RevisionPersistenceError(
                "Ordinary save cannot downgrade a succeeded tool call."
            )


def _is_collection_expansion(stored: ExecutionState, incoming: ExecutionState) -> bool:
    """One stored collection template replaced by its expanded concrete steps."""
    stored_ids = set(_step_ids(stored.plan))
    expanded_from = dict(incoming.collections.expanded_from or {})
    replaced: str | None = None
    i = 0
    j = 0
    while i < len(stored.plan) and j < len(incoming.plan):
        before = stored.plan[i]
        after = incoming.plan[j]
        if before.step_id == after.step_id:
            i += 1
            j += 1
            continue
        if before.collection is None or replaced is not None:
            return False
        replaced = before.step_id
        i += 1
        produced = 0
        while j < len(incoming.plan) and incoming.plan[j].step_id not in stored_ids:
            child = incoming.plan[j]
            if child.collection is not None:
                return False
            if expanded_from.get(child.step_id) != before.step_id:
                return False
            if not child.step_id.startswith(f"{before.step_id}__"):
                return False
            produced += 1
            j += 1
        if produced < 1:
            return False
    return i == len(stored.plan) and j == len(incoming.plan) and replaced is not None


def _step_ids(steps: list[PlanStep]) -> list[str]:
    return [step.step_id for step in steps]


def _step_payload(step: PlanStep) -> dict[str, Any]:
    collection = None
    if step.collection is not None:
        collection = step.collection.model_dump(mode="json")
    return {
        "step_id": step.step_id,
        "tool": step.resolved_tool_name(),
        "operation": operation_of(step.arguments or {}),
        "arguments": step.arguments or {},
        "collection": collection,
        "status": step.status.value if step.status is not PlanStepStatus.PENDING else "pending",
    }
