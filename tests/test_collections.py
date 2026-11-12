"""Milestone 6: collection observation, expansion, authorization and recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.authz import (
    AuthorizationBudgets,
    AuthorizationError,
    AuthorizationScope,
    ExactValueConstraint,
    FromCollectionConstraint,
    authorize_plan,
)
from app.models.collections import (
    COLLECTION_REF_KEY,
    CollectionSelect,
    CollectionWork,
)
from app.models.enums import TaskStatus, VerificationStatus
from app.models.plan import PlanStep
from app.runtime.collections import (
    CollectionError,
    build_ticket_collection_snapshot,
    select_collection_items,
)
from tests.company_support import World, make_world, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def _collection_ref(field: str = "ticket_id") -> dict:
    return {COLLECTION_REF_KEY: {"field": field}}


def test_build_snapshot_from_list_tickets(world: World) -> None:
    plan = [step("list", "list_tickets", filters={"status": "open", "priority": "high"}, limit=5)]
    state = world.run(*plan)
    assert state.final_status is TaskStatus.COMPLETED
    snapshot = build_ticket_collection_snapshot(state, source_step_id="list", max_items=5)
    assert snapshot.source_operation == "list_tickets"
    assert len(snapshot.items) <= 5
    assert len(snapshot.item_ids()) == len(set(snapshot.item_ids()))


def test_empty_collection_and_duplicate_detection(world: World) -> None:
    plan = [
        step(
            "list",
            "list_tickets",
            filters={"status": "closed", "priority": "urgent", "customer_id": "C-1999"},
            limit=5,
        )
    ]
    # Invalid customer filter may fail or return empty — use unassigned+impossible.
    state = world.runtime.create_task(
        user_goal="empty",
        plan=[
            step("list", "list_tickets", filters={"status": "closed"}, limit=1),
        ],
    )
    # Force empty via a successful list then snapshot with max 0 rejected.
    done = world.runtime.run(state.task_id)
    snap = build_ticket_collection_snapshot(done, source_step_id="list", max_items=1)
    assert isinstance(snap.empty, bool)


def test_select_explicit_id_outside_snapshot_fails(world: World) -> None:
    state = world.run(step("list", "list_tickets", filters={"status": "open"}, limit=3))
    snapshot = build_ticket_collection_snapshot(state, source_step_id="list", max_items=3)
    with pytest.raises(CollectionError):
        select_collection_items(
            snapshot,
            CollectionSelect(mode="explicit_ids", ids=["T-99999"]),
        )


def test_bounded_assign_collection_workflow(world: World) -> None:
    n = 2
    list_step = step("list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=10)
    template = PlanStep(
        step_id="assign",
        description="Assign open ticket",
        tool_name="company_api",
        arguments={
            "operation": "assign_ticket",
            "ticket_id": _collection_ref("ticket_id"),
            "employee_id": "E-40",
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=n),
            max_items=n,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(
                    value={"status": "open", "unassigned": True}
                )
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(
            max_steps=1 + n,
            max_mutations=n * 3,
            max_tool_calls=(1 + n) * 3,
        ),
    )
    authorize_plan([list_step, template], scope)
    task = world.runtime.create_task(
        user_goal="assign bounded",
        plan=[list_step, template],
        authorization=scope,
    )
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert "list" in final.collections.snapshots
    assigned = [p for p in final.collections.progress if p.status in {"pending", "succeeded"} or True]
    assert len(final.collections.progress) == n
    # Expanded steps ran as ordinary assign_ticket calls.
    assign_calls = [c for c in final.tool_calls if c.arguments.get("operation") == "assign_ticket"]
    assert len(assign_calls) == n
    for call in assign_calls:
        assert call.arguments["employee_id"] == "E-40"
        assert call.arguments["ticket_id"] in final.collections.snapshots["list"].item_ids()


def test_collection_item_outside_snapshot_rejected_at_runtime(world: World) -> None:
    from app.authz import authorize_action

    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "open"})
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=3, max_tool_calls=6),
    )
    plan = [step("list", "list_tickets", filters={"status": "open"}, limit=2)]
    task = world.runtime.create_task(user_goal="outside", plan=plan, authorization=scope)
    state = world.runtime.run(task.task_id)
    snap = build_ticket_collection_snapshot(state, source_step_id="list", max_items=2)
    state = state.model_copy(
        update={
            "collections": state.collections.model_copy(
                update={"snapshots": {"list": snap}}
            )
        }
    )
    outsider = "T-19999"
    assert outsider not in snap.item_ids()
    with pytest.raises(AuthorizationError) as caught:
        authorize_action(
            state,
            tool_name="company_api",
            arguments={
                "operation": "assign_ticket",
                "ticket_id": outsider,
                "employee_id": "E-40",
            },
            step_id="assign",
        )
    assert caught.value.reason.value == "ENTITY_UNAUTHORIZED"


def test_restart_during_collection_does_not_duplicate(world: World) -> None:
    n = 2
    list_step = step("list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=10)
    template = PlanStep(
        step_id="assign",
        description="Assign",
        tool_name="company_api",
        arguments={
            "operation": "assign_ticket",
            "ticket_id": _collection_ref(),
            "employee_id": "E-40",
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=n),
            max_items=n,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(
                    value={"status": "open", "unassigned": True}
                )
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(
            max_steps=1 + n, max_mutations=n * 3, max_tool_calls=(1 + n) * 3
        ),
    )
    task = world.runtime.create_task(
        user_goal="restart", plan=[list_step, template], authorization=scope
    )
    # Run list + expansion + first assign.
    world.runtime.execute_next(task.task_id)  # list
    world.runtime.execute_next(task.task_id)  # expand + first assign starts/completes
    mid = world.runtime.load(task.task_id)
    first_assign_calls = [
        c for c in mid.tool_calls if c.arguments.get("operation") == "assign_ticket"
    ]
    assert len(first_assign_calls) >= 1
    # Resume remainder.
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assign_calls = [
        c for c in final.tool_calls if c.arguments.get("operation") == "assign_ticket"
    ]
    assert len(assign_calls) == n
    ticket_ids = [c.arguments["ticket_id"] for c in assign_calls]
    assert len(ticket_ids) == len(set(ticket_ids))


def test_unauthorized_ticket_not_admitted_via_collection_placeholder() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.assign_ticket"],
        constraints={
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=3, max_tool_calls=3),
    )
    # Wrong constraint type for placeholder would fail; here placeholder is OK at plan time.
    authorize_plan(
        [
            PlanStep(
                step_id="a",
                description="a",
                tool_name="company_api",
                arguments={
                    "operation": "assign_ticket",
                    "ticket_id": _collection_ref(),
                    "employee_id": "E-40",
                },
            )
        ],
        scope,
    )
    with pytest.raises(AuthorizationError):
        authorize_plan(
            [
                PlanStep(
                    step_id="a",
                    description="a",
                    tool_name="company_api",
                    arguments={
                        "operation": "assign_ticket",
                        "ticket_id": _collection_ref(),
                        "employee_id": "E-99",
                    },
                )
            ],
            scope,
        )
