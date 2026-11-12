"""Deterministic plans for the trusted collection grammars.

The model is not asked to choose steps, collection paths, item fields or
iteration. Paths are the registered ``list_tickets`` snapshot (``tickets`` /
``ticket_id``). Explicit-id status updates are ordinary steps, one per supplied
id, which is a tighter authorization than a collection placeholder.
"""

from __future__ import annotations

from app.models.collections import CollectionSelect, CollectionWork
from app.models.plan import Plan, PlanStep
from app.planning.intents import COLLECTION_INTENT_IDS, TrustedIntent
from app.planning.planner import PlanningError
from app.world.models import TICKET_TRANSITIONS

_REGISTERED_ITEMS_PATH = ["tickets"]
_REGISTERED_ITEM_FIELD = "ticket_id"
_TRANSITION_TARGETS = frozenset().union(*TICKET_TRANSITIONS.values())


def plan_for_collection_intent(intent: TrustedIntent) -> Plan:
    """Build the only plan the catalog will accept for this intent."""
    if intent.intent_id not in COLLECTION_INTENT_IDS:
        raise PlanningError(
            f"Intent {intent.intent_id!r} is not a deterministic collection workflow."
        )
    if intent.intent_id == "assign_bounded_tickets":
        return Plan(steps=_assignment_steps(intent))
    if intent.intent_id == "list_bounded_tickets":
        return Plan(steps=[_list_step(intent)])
    if intent.intent_id == "update_ticket_status_batch":
        return Plan(steps=_status_steps(intent))
    raise PlanningError(
        f"Intent {intent.intent_id!r} has no deterministic collection plan."
    )


def _filters(intent: TrustedIntent) -> dict[str, str]:
    status = str(intent.entities["status"])
    priority = str(intent.entities["priority"])
    return {"status": status, "priority": priority}


def _bound(intent: TrustedIntent) -> int:
    value = intent.entities.get("bound")
    if type(value) is not int or value < 1:
        raise PlanningError("Collection bound is missing.")
    return value


def _list_step(intent: TrustedIntent) -> PlanStep:
    bound = _bound(intent)
    filters = _filters(intent)
    step_id = str(intent.entities.get("list_step_id") or "list")
    status = filters["status"]
    priority = filters["priority"]
    return PlanStep(
        step_id=step_id,
        description=f"List up to {bound} {status} {priority}-priority tickets.",
        tool_name="company_api",
        arguments={
            "operation": "list_tickets",
            "filters": filters,
            "limit": bound,
            "offset": 0,
        },
        expected_outcome=(
            f"At most {bound} {status} {priority}-priority tickets."
        ),
    )


def _assignment_steps(intent: TrustedIntent) -> list[PlanStep]:
    bound = _bound(intent)
    employee_id = str(intent.entities["employee_id"])
    listing = _list_step(intent)
    return [
        listing,
        PlanStep(
            step_id="assign",
            description=f"Assign each selected ticket to {employee_id}.",
            tool_name="company_api",
            arguments={
                "operation": "assign_ticket",
                "ticket_id": {"$collection_item": {"field": _REGISTERED_ITEM_FIELD}},
                "employee_id": employee_id,
            },
            expected_outcome=f"Ticket assigned to {employee_id}.",
            collection=CollectionWork(
                from_step=listing.step_id,
                items_path=list(_REGISTERED_ITEMS_PATH),
                item_id_field=_REGISTERED_ITEM_FIELD,
                select=CollectionSelect(mode="first_n", n=bound),
                max_items=bound,
            ),
        ),
    ]


def _status_steps(intent: TrustedIntent) -> list[PlanStep]:
    status = str(intent.entities.get("status") or "")
    if status not in _TRANSITION_TARGETS:
        raise PlanningError(f"Status {status!r} is not a legal ticket transition target.")
    raw_ids = intent.entities.get("ticket_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise PlanningError("Batch status update has no ticket ids.")
    steps: list[PlanStep] = []
    for index, ticket_id in enumerate(raw_ids, start=1):
        ticket = str(ticket_id)
        steps.append(
            PlanStep(
                step_id=f"update_{index}",
                description=f"Set ticket {ticket} status to {status}.",
                tool_name="company_api",
                arguments={
                    "operation": "update_ticket",
                    "ticket_id": ticket,
                    "fields": {"status": status},
                },
                expected_outcome=f"Ticket {ticket} status is {status}.",
            )
        )
    return steps
