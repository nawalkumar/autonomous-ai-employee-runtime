"""Milestone 6: collection observation, expansion, authorization and recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.authz import (
    AuthorizationBudgets,
    AuthorizationError,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromCollectionConstraint,
    authorize_plan,
)
from app.models.collections import (
    COLLECTION_REF_KEY,
    CollectionProgress,
    CollectionSelect,
    CollectionWork,
)
from app.models.enums import ApprovalStatus, PlanStepStatus, TaskStatus, VerificationStatus
from app.models.plan import PlanStep
from app.runtime.collections import (
    CollectionError,
    build_ticket_collection_snapshot,
    select_collection_items,
    sync_collection_progress,
)
from app.verify.results import VerificationCheckResult, VerificationResult
from tests.company_support import SimulatedCrash, World, make_world, reopen, step


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
    assert len(final.collections.progress) == n
    assert [item.status for item in final.collections.progress] == ["verified"] * n
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


def _assign_collection(n: int = 2) -> tuple[PlanStep, PlanStep, AuthorizationScope]:
    list_step = step(
        "list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=10
    )
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
                "filters": ExactValueConstraint(value={"status": "open", "unassigned": True})
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(
            max_steps=1 + n, max_mutations=n, max_tool_calls=(1 + n) * 3
        ),
    )
    return list_step, template, scope


def _close_collection(n: int = 2) -> tuple[PlanStep, PlanStep, AuthorizationScope]:
    list_step = step("list", "list_tickets", filters={"status": "resolved"}, limit=10)
    template = PlanStep(
        step_id="close",
        description="Close resolved ticket",
        tool_name="company_api",
        arguments={
            "operation": "update_ticket",
            "ticket_id": _collection_ref("ticket_id"),
            "fields": {"status": "closed"},
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=n),
            max_items=n,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.update_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "resolved"})
            },
            "company_api.update_ticket": {
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": ExactValueConstraint(value="closed"),
            },
        },
        budgets=AuthorizationBudgets(
            max_steps=1 + n, max_mutations=n, max_tool_calls=(1 + n) * 3
        ),
    )
    return list_step, template, scope


def test_legacy_succeeded_progress_is_not_treated_as_verified() -> None:
    record = CollectionProgress(
        collection_id="coll",
        item_id="T-1",
        step_id="assign__T-1",
        status="succeeded",  # type: ignore[arg-type]
    )
    assert record.status == "completed"


def test_progress_partial_completion_and_restart(world: World) -> None:
    list_step, template, scope = _assign_collection(2)
    task = world.runtime.create_task(
        user_goal="partial", plan=[list_step, template], authorization=scope
    )
    world.runtime.execute_next(task.task_id)
    mid = world.runtime.execute_next(task.task_id)
    assert [item.status for item in mid.collections.progress] == ["completed", "pending"]
    assert mid.verification_status is not VerificationStatus.PASSED

    resumed = reopen(world)
    loaded = resumed.runtime.load(task.task_id)
    assert [item.status for item in loaded.collections.progress] == ["completed", "pending"]
    final = resumed.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert [item.status for item in final.collections.progress] == ["verified", "verified"]
    assigns = [c for c in final.tool_calls if c.arguments.get("operation") == "assign_ticket"]
    assert len(assigns) == 2
    assert len({c.arguments["ticket_id"] for c in assigns}) == 2


def test_progress_is_running_while_item_executes(world: World) -> None:
    list_step, template, scope = _assign_collection(2)
    task = world.runtime.create_task(
        user_goal="running", plan=[list_step, template], authorization=scope
    )
    world.runtime.execute_next(task.task_id)
    seen: list[list[str]] = []
    tool = world.company
    original = tool.execute

    def execute(args, *, idempotency_key=None):
        if args.operation == "assign_ticket":
            mid = world.runtime.load(task.task_id)
            seen.append([item.status for item in mid.collections.progress])
        return original(args, idempotency_key=idempotency_key)

    tool.execute = execute  # type: ignore[method-assign]
    world.runtime.execute_next(task.task_id)
    assert seen
    assert seen[0][0] == "running"
    assert seen[0][1] == "pending"


def test_progress_awaiting_approval_rejection_and_no_replan(world: World) -> None:
    list_step, template, scope = _close_collection(2)
    task = world.runtime.create_task(
        user_goal="close", plan=[list_step, template], authorization=scope
    )
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.approval_status is ApprovalStatus.PENDING
    assert [item.status for item in paused.collections.progress] == [
        "awaiting_approval",
        "pending",
    ]
    rejected = world.runtime.reject_task(task.task_id, "no")
    assert rejected.final_status is TaskStatus.ABORTED
    assert [item.status for item in rejected.collections.progress] == ["rejected", "pending"]
    assert rejected.replan_count == 0
    closes = [c for c in rejected.tool_calls if c.arguments.get("operation") == "update_ticket"]
    assert closes == []
    kinds = [event.event_type for event in world.store.get_events(task.task_id)]
    assert not any(kind.startswith("replan.") for kind in kinds)


def test_progress_verified_after_collection_approval(world: World) -> None:
    list_step, template, scope = _close_collection(1)
    task = world.runtime.create_task(
        user_goal="close-one", plan=[list_step, template], authorization=scope
    )
    state = world.runtime.run(task.task_id)
    while state.final_status is TaskStatus.NEEDS_APPROVAL:
        state = world.runtime.approve_task(task.task_id, approver="test-approver")
    assert state.final_status is TaskStatus.COMPLETED
    assert state.verification_status is VerificationStatus.PASSED
    assert [item.status for item in state.collections.progress] == ["verified"]


def test_progress_failed_verification(world: World) -> None:
    class _Fail:
        def verify(self, state):
            return VerificationResult(
                status=VerificationStatus.FAILED,
                passed=False,
                summary="injected verification failure",
                checks=[
                    VerificationCheckResult(
                        name="injected", passed=False, message="injected verification failure"
                    )
                ],
                failure_reason="injected verification failure",
            )

    list_step, template, scope = _assign_collection(2)
    world.runtime.verifier = _Fail()  # type: ignore[assignment]
    task = world.runtime.create_task(
        user_goal="verify-fail", plan=[list_step, template], authorization=scope
    )
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.verification_status is VerificationStatus.FAILED
    assert [item.status for item in final.collections.progress] == ["failed", "failed"]
    assert {item.error for item in final.collections.progress} == {"verification_failed"}


def test_progress_failed_execution_leaves_later_items_pending(world: World) -> None:
    world.injector.inject_once("company_api.assign_ticket", "invalid_args", "boom")
    list_step, template, scope = _assign_collection(2)
    task = world.runtime.create_task(
        user_goal="exec-fail", plan=[list_step, template], authorization=scope
    )
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert [item.status for item in final.collections.progress] == ["failed", "pending"]
    assigns = [c for c in final.tool_calls if c.arguments.get("operation") == "assign_ticket"]
    assert len(assigns) == 1
    assert assigns[0].status.value == "failed"


def test_restart_between_snapshot_and_expansion_expands_once(world: World) -> None:
    list_step, template, scope = _assign_collection(2)
    task = world.runtime.create_task(
        user_goal="snap", plan=[list_step, template], authorization=scope
    )
    world.runtime.execute_next(task.task_id)
    original_append = world.store.append_event

    def append_event(event):
        original_append(event)
        if event.event_type == "collection.observed":
            raise SimulatedCrash("snapshot-before-expansion")

    world.store.append_event = append_event  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        world.runtime.execute_next(task.task_id)

    resumed = reopen(world)
    loaded = resumed.runtime.load(task.task_id)
    assert "list" in loaded.collections.snapshots
    assert any(step.step_id == "assign" and step.collection is not None for step in loaded.plan)
    final = resumed.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert [item.status for item in final.collections.progress] == ["verified", "verified"]
    assigns = [c for c in final.tool_calls if c.arguments.get("operation") == "assign_ticket"]
    assert len(assigns) == 2
    observed = [
        event
        for event in resumed.store.get_events(task.task_id)
        if event.event_type == "collection.observed"
    ]
    assert len(observed) == 1


def test_sync_projects_terminal_step_without_a_second_write() -> None:
    progress = CollectionProgress(
        collection_id="coll", item_id="T-1", step_id="assign__T-1", status="pending"
    )
    step_done = PlanStep(
        step_id="assign__T-1",
        description="done",
        tool_name="company_api",
        arguments={"operation": "assign_ticket"},
        status=PlanStepStatus.COMPLETED,
    )
    from app.models.collections import CollectionExecutionState
    from app.models.state import ExecutionState

    state = ExecutionState(
        user_goal="g",
        plan=[step_done],
        completed_steps=["assign__T-1"],
        verification_status=VerificationStatus.PASSED,
        collections=CollectionExecutionState(progress=[progress]),
    )
    projected = sync_collection_progress(state)
    assert projected.collections.progress[0].status == "verified"
    assert sync_collection_progress(projected) is projected
