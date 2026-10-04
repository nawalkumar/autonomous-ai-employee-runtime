"""Phase 7 human-in-the-loop approval / pause / resume tests."""

from pathlib import Path

import pytest

from app.models import (
    ApprovalStatus,
    PlanStep,
    PlanStepStatus,
    TaskStatus,
    VerificationStatus,
)
from app.models.enums import FailureType
from app.policy import ApprovalPolicy, PolicyDecision
from app.runtime import ApprovalError, EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.tools.base import RiskLevel
from app.world import CompanyRepository


def _employee_update_plan() -> list[PlanStep]:
    return [
        PlanStep(
            step_id="step_1",
            description="Read employee E-17",
            tool_name="company_api",
            arguments={"operation": "get_employee", "employee_id": "E-17"},
        ),
        PlanStep(
            step_id="step_2",
            description="Update employee title",
            tool_name="company_api",
            arguments={
                "operation": "update_employee",
                "employee_id": "E-17",
                "fields": {"title": "Senior Engineer"},
            },
        ),
        PlanStep(
            step_id="step_3",
            description="Write confirmation note",
            tool_name="file",
            arguments={
                "operation": "write",
                "path": "employee_update.txt",
                "content": "E-17 title updated to Senior Engineer",
            },
        ),
    ]


def test_low_risk_action_does_not_pause(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Look up employee E-17",
        plan=[
            PlanStep(
                step_id="read",
                description="Get employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            )
        ],
    )

    final = runtime.run(state.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.approval_status is ApprovalStatus.NONE
    assert final.pending_action is None
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].status.value == "succeeded"
    assert company_repo.get_employee("E-17") is not None


def test_high_risk_action_pauses_before_mutation(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    before = company_repo.get_employee("E-17")
    assert before is not None
    original_title = before.title

    state = runtime.create_task(
        user_goal="Update employee E-17 title",
        plan=[
            PlanStep(
                step_id="upd",
                description="Update title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            )
        ],
    )

    paused = runtime.run(state.task_id)

    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.approval_status is ApprovalStatus.PENDING
    assert paused.pending_action is not None
    assert paused.pending_action.tool_name == "company_api"
    assert paused.pending_action.arguments["operation"] == "update_employee"
    assert paused.pending_action.arguments["employee_id"] == "E-17"
    assert paused.pending_action.arguments["fields"]["title"] == "Senior Engineer"
    assert paused.pending_action.risk == RiskLevel.WRITE.value
    assert "approval" in (paused.pending_action.reason or "").lower()
    assert paused.plan[0].status is PlanStepStatus.PENDING
    assert len(paused.tool_calls) == 0
    assert company_repo.get_employee("E-17").title == original_title

    types = [e.event_type for e in store.get_events(state.task_id)]
    assert EventType.APPROVAL_REQUESTED in types


def test_approval_resumes_from_pending_step_exactly_once(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update employee E-17's title to Senior Engineer and write a confirmation note.",
        plan=_employee_update_plan(),
    )

    paused = runtime.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.plan[0].status is PlanStepStatus.COMPLETED
    assert paused.plan[1].status is PlanStepStatus.PENDING
    assert paused.plan[2].status is PlanStepStatus.PENDING
    assert len(paused.tool_calls) == 1
    assert paused.tool_calls[0].step_id == "step_1"
    assert company_repo.get_employee("E-17").title == "Software Engineer"

    final = runtime.approve_task(state.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.evidence
    assert [s.status for s in final.plan] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    # Step 1 must not have been executed again.
    assert [c.step_id for c in final.tool_calls] == ["step_1", "step_2", "step_3"]
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    assert (workspace / "employee_update.txt").exists()


def test_rejection_does_not_mutate_state(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update E-17",
        plan=[
            PlanStep(
                step_id="upd",
                description="Update title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            )
        ],
    )

    paused = runtime.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL

    rejected = runtime.reject_task(state.task_id, reason="Manager declined title change")

    assert rejected.final_status is TaskStatus.ABORTED
    assert rejected.approval_status is ApprovalStatus.REJECTED
    assert rejected.completion_summary == "Manager declined title change"
    assert len(rejected.tool_calls) == 0
    assert company_repo.get_employee("E-17").title == "Software Engineer"
    assert not (workspace / "employee_update.txt").exists()

    types = [e.event_type for e in store.get_events(state.task_id)]
    assert EventType.APPROVAL_REQUESTED in types
    assert EventType.APPROVAL_REJECTED in types


def test_persistence_across_runtime_restart(
    db_path: Path,
    workspace: Path,
    failure_injector: FailureInjector,
) -> None:
    store1 = SQLiteStore(db_path)
    repo = CompanyRepository(db_path)
    registry1 = ToolRegistry()
    registry1.register(CompanyAPITool(repo, failure_injector=failure_injector))
    registry1.register(FileTool(workspace))
    runtime1 = ExecutionRuntime(
        store=store1, registry=registry1, workspace_path=workspace
    )

    state = runtime1.create_task(
        user_goal="Update E-17 and confirm",
        plan=_employee_update_plan(),
    )
    paused = runtime1.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action is not None
    assert paused.approval_status is ApprovalStatus.PENDING

    store2 = SQLiteStore(db_path)
    registry2 = ToolRegistry()
    registry2.register(
        CompanyAPITool(CompanyRepository(db_path), failure_injector=failure_injector)
    )
    registry2.register(FileTool(workspace))
    runtime2 = ExecutionRuntime(
        store=store2, registry=registry2, workspace_path=workspace
    )

    loaded = runtime2.load(state.task_id)
    assert loaded.final_status is TaskStatus.NEEDS_APPROVAL
    assert loaded.pending_action is not None
    assert loaded.approval_status is ApprovalStatus.PENDING
    assert CompanyRepository(db_path).get_employee("E-17").title == "Software Engineer"

    final = runtime2.approve_task(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert CompanyRepository(db_path).get_employee("E-17").title == "Senior Engineer"
    assert (workspace / "employee_update.txt").exists()


def test_double_approval_is_safe(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update E-17",
        plan=_employee_update_plan(),
    )

    runtime.run(state.task_id)
    final = runtime.approve_task(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    update_calls = [c for c in final.tool_calls if c.step_id == "step_2"]
    assert len(update_calls) == 1

    with pytest.raises(ApprovalError, match="no pending approval"):
        runtime.approve_task(state.task_id)

    after = runtime.load(state.task_id)
    update_calls_after = [c for c in after.tool_calls if c.step_id == "step_2"]
    assert len(update_calls_after) == 1
    assert company_repo.get_employee("E-17").title == "Senior Engineer"


def test_rejected_task_cannot_be_approved(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update E-17",
        plan=[
            PlanStep(
                step_id="upd",
                description="Update title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            )
        ],
    )

    runtime.run(state.task_id)
    runtime.reject_task(state.task_id, reason="No")

    with pytest.raises(ApprovalError, match="no pending approval"):
        runtime.approve_task(state.task_id)

    after = runtime.load(state.task_id)
    assert after.final_status is TaskStatus.ABORTED
    assert after.approval_status is ApprovalStatus.REJECTED
    assert len(after.tool_calls) == 0
    assert company_repo.get_employee("E-17").title == "Software Engineer"


def test_audit_trail_approval_and_rejection_paths(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)

    approved_task = runtime.create_task(
        user_goal="Approve path",
        plan=_employee_update_plan(),
    )
    runtime.run(approved_task.task_id)
    runtime.approve_task(approved_task.task_id)
    approved_types = [e.event_type for e in store.get_events(approved_task.task_id)]
    assert EventType.APPROVAL_REQUESTED in approved_types
    assert EventType.APPROVAL_APPROVED in approved_types
    assert EventType.TOOL_CALLED in approved_types
    assert EventType.TOOL_SUCCEEDED in approved_types
    assert EventType.VERIFICATION_STARTED in approved_types
    assert EventType.TASK_COMPLETED in approved_types
    req_idx = approved_types.index(EventType.APPROVAL_REQUESTED)
    appr_idx = approved_types.index(EventType.APPROVAL_APPROVED)
    assert req_idx < appr_idx
    # Sensitive tool call happens only after approval.
    post_approval = approved_types[appr_idx:]
    assert EventType.TOOL_CALLED in post_approval
    assert EventType.VERIFICATION_STARTED in post_approval


    rejected_task = runtime.create_task(
        user_goal="Reject path",
        plan=[
            PlanStep(
                step_id="upd",
                description="Update title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-23",
                    "fields": {"title": "Director"},
                },
            )
        ],
    )
    runtime.run(rejected_task.task_id)
    runtime.reject_task(rejected_task.task_id, reason="Denied")
    rejected_types = [e.event_type for e in store.get_events(rejected_task.task_id)]
    assert EventType.APPROVAL_REQUESTED in rejected_types
    assert EventType.APPROVAL_REJECTED in rejected_types
    assert EventType.TOOL_CALLED not in rejected_types


def test_recovery_still_works_after_approval(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    # Inject a pre-mutation transient failure on the approved update step.
    failure_injector.inject_once(
        operation="company_api.update_employee",
        error_type=FailureType.TRANSIENT.value,
        error_message="Mock company API temporarily unavailable",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update E-17 with recovery",
        plan=_employee_update_plan(),
    )

    paused = runtime.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert company_repo.get_employee("E-17").title == "Software Engineer"

    final = runtime.approve_task(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    assert (workspace / "employee_update.txt").exists()
    update_calls = [c for c in final.tool_calls if c.step_id == "step_2"]
    assert len(update_calls) == 2  # failed once, then succeeded
    assert any(
        e.event_type == EventType.RECOVERY_RETRY_STARTED
        for e in store.get_events(state.task_id)
    )


def test_policy_decision_contract() -> None:
    policy = ApprovalPolicy()
    yes = policy.evaluate(
        tool_name="company_api",
        arguments={
            "operation": "update_employee",
            "employee_id": "E-17",
            "fields": {"title": "Senior Engineer"},
        },
    )
    assert isinstance(yes, PolicyDecision)
    assert yes.requires_approval is True
    assert yes.risk is RiskLevel.WRITE

    no = policy.evaluate(
        tool_name="company_api",
        arguments={"operation": "get_employee", "employee_id": "E-17"},
    )
    assert no.requires_approval is False

    file_write = policy.evaluate(
        tool_name="file",
        arguments={"operation": "write", "path": "a.txt", "content": "x"},
    )
    assert file_write.requires_approval is False
