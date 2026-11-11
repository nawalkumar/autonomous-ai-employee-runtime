"""v0.3.0 milestone 0: employee-update validation, verification hardening,
and approvals bound to the exact action."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cli import build_runtime
from app.config import Settings
from app.main import create_app
from app.models.enums import (
    ApprovalStatus,
    FailureType,
    PlanStepStatus,
    TaskStatus,
    ToolCallStatus,
    VerificationStatus,
)
from app.models.goal import SuccessCriterion
from app.models.plan import Plan, PlanStep
from app.models.records import ToolCallRecord
from app.models.state import ExecutionState
from app.planning.validator import PlanValidationError, validate_plan
from app.runtime import (
    EventType,
    ExecutionRuntime,
    StaleApprovalError,
    action_fingerprint,
    pending_fingerprint,
)
from app.store import SQLiteStore
from app.tools import FailureInjector, ToolRegistry
from app.verify import OutcomeVerifier
from app.world import CompanyRepository, InvalidEmployeeUpdateError
from tests.api_support import EMPLOYEE_GOAL, submit, wait_for

UPDATE_ARGS = {
    "operation": "update_employee",
    "employee_id": "E-17",
    "fields": {"title": "Senior Engineer"},
}


def update_step(fields: dict[str, Any] | None = None, step_id: str = "step_1") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Update employee E-17",
        tool_name="company_api",
        arguments={**UPDATE_ARGS, "fields": fields or {"title": "Senior Engineer"}},
    )


def employee_row(db_path: Path, employee_id: str = "E-17") -> tuple:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM company_employees WHERE employee_id = ?", (employee_id,)
        ).fetchone()
    finally:
        conn.close()


def idempotency_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM company_idempotency").fetchone()[0]
    finally:
        conn.close()


def event_types(store: SQLiteStore, task_id: str) -> list[str]:
    return [e.event_type for e in store.get_events(task_id)]


# --- Task 1: employee updates are applied exactly or rejected -----------------------


INVALID_FIELD_MAPS = [
    pytest.param({"salary": 150000}, id="unknown-field"),
    pytest.param({"title": "Senior Engineer", "salary": 150000}, id="mixed-valid-invalid"),
    pytest.param({}, id="empty"),
    pytest.param({"title": None}, id="null-value"),
    pytest.param({"title": 42}, id="non-string"),
    pytest.param({"title": "   "}, id="blank"),
    pytest.param({"employee_id": "E-99"}, id="primary-key"),
    pytest.param({"updated_at": "2020-01-01"}, id="system-column"),
]


@pytest.mark.parametrize("fields", INVALID_FIELD_MAPS)
def test_tool_rejects_invalid_update_without_any_write(
    registry: ToolRegistry, db_path: Path, fields: dict
) -> None:
    before = employee_row(db_path)
    result = registry.get("company_api").invoke(
        {**UPDATE_ARGS, "fields": fields}, idempotency_key="task_x:step_1"
    )
    assert result.ok is False
    assert result.error_type == FailureType.INVALID_ARGS.value
    assert employee_row(db_path) == before
    assert idempotency_rows(db_path) == 0


@pytest.mark.parametrize("fields", INVALID_FIELD_MAPS)
def test_repository_rejects_invalid_update_without_any_write(
    company_repo: CompanyRepository, db_path: Path, fields: dict
) -> None:
    before = employee_row(db_path)
    with pytest.raises(InvalidEmployeeUpdateError):
        company_repo.update_employee_idempotent("E-17", fields, idempotency_key="k-1")
    assert employee_row(db_path) == before
    assert idempotency_rows(db_path) == 0
    assert company_repo.get_idempotency_record("k-1") is None


def test_rejection_names_the_unsupported_fields(registry: ToolRegistry) -> None:
    result = registry.get("company_api").invoke(
        {**UPDATE_ARGS, "fields": {"title": "X", "salary": 1, "bonus": 2}}
    )
    assert "bonus, salary" in result.error_message


@pytest.mark.parametrize("field", ["name", "title", "email", "department"])
def test_each_supported_field_still_updates(
    registry: ToolRegistry, company_repo: CompanyRepository, field: str
) -> None:
    value = "ops@example.test" if field == "email" else f"New {field}"
    result = registry.get("company_api").invoke(
        {**UPDATE_ARGS, "fields": {field: value}}, idempotency_key=f"t:{field}"
    )
    assert result.ok, result.error_message
    assert getattr(company_repo.get_employee("E-17"), field) == value


def test_valid_update_replay_is_unchanged(company_repo: CompanyRepository, db_path: Path) -> None:
    first, replayed_first = company_repo.update_employee_idempotent(
        "E-17", {"title": "Staff Engineer", "department": "Platform"}, idempotency_key="k-1"
    )
    second, replayed_second = company_repo.update_employee_idempotent(
        "E-17", {"department": "Platform", "title": "Staff Engineer"}, idempotency_key="k-1"
    )
    assert (replayed_first, replayed_second) == (False, True)
    assert first == second
    assert idempotency_rows(db_path) == 1


def test_plan_validation_rejects_unsupported_employee_fields(registry: ToolRegistry) -> None:
    plan = Plan(steps=[update_step({"title": "Senior Engineer", "salary": 1})])
    with pytest.raises(PlanValidationError, match="salary"):
        validate_plan(plan, registry)


def test_approved_unknown_field_update_fails_and_is_not_retried(
    runtime: ExecutionRuntime, store: SQLiteStore, db_path: Path
) -> None:
    before = employee_row(db_path)
    task = runtime.create_task(user_goal="Raise salary", plan=[update_step({"salary": 150000})])
    paused = runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL

    final = runtime.approve_task(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.verification_status is not VerificationStatus.PASSED
    assert [c.error_type for c in final.tool_calls] == [FailureType.INVALID_ARGS.value]
    assert final.retry_count == 0
    assert employee_row(db_path) == before
    assert idempotency_rows(db_path) == 0


# --- Task 2: verification never passes on what it cannot evaluate --------------------


def verified_state(
    criteria: list[SuccessCriterion],
    *,
    plan: list[PlanStep] | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
) -> ExecutionState:
    return ExecutionState(
        user_goal="verification probe",
        success_criteria=criteria,
        plan=plan or [],
        tool_calls=tool_calls or [],
        final_status=TaskStatus.RUNNING,
    )


def criterion(ctype: str, expected: Any) -> SuccessCriterion:
    return SuccessCriterion(description=f"{ctype} probe", criterion_type=ctype, expected_value=expected)


@pytest.fixture
def updated_e17(company_repo: CompanyRepository) -> None:
    company_repo.update_employee("E-17", {"title": "Senior Engineer"})


def test_supported_criterion_passes(verifier: OutcomeVerifier, updated_e17: None) -> None:
    result = verifier.verify(
        verified_state([criterion("employee_field", {"employee_id": "E-17", "title": "Senior Engineer"})])
    )
    assert result.status is VerificationStatus.PASSED
    assert [c.name for c in result.checks] == ["employee_field"]


@pytest.mark.parametrize(
    "ctype,expected,reason",
    [
        ("employee_field", {"employee_id": "E-17", "salary": 150000}, "salary"),
        ("employee_field", {"employee_id": "E-17", "title": "Senior Engineer", "bonus": 1}, "bonus"),
        ("employee_field", {"employee_id": "E-17"}, "names no field"),
        ("ticket_exists", {"customer_id": "C-1001", "priority": "high"}, "priority"),
        ("file_exists", {"path": "a.txt", "mode": "0600"}, "mode"),
        ("step_completed", {"step_id": "step_1", "args": {}}, "args"),
        ("record_exists", {"customer": "Acme"}, "no verification rule"),
        ("employee_field", "Senior Engineer", "value"),
    ],
)
def test_unsupported_criteria_fail_with_a_reason(
    verifier: OutcomeVerifier, updated_e17: None, ctype: str, expected: Any, reason: str
) -> None:
    result = verifier.verify(verified_state([criterion(ctype, expected)]))
    assert result.status is VerificationStatus.FAILED and result.passed is False
    (check,) = result.checks
    assert check.name == "unsupported_criterion" and check.passed is False
    assert check.message.startswith("Unsupported criterion:")
    assert reason in check.message


def test_mixed_supported_and_unsupported_criteria_fail(
    verifier: OutcomeVerifier, updated_e17: None
) -> None:
    result = verifier.verify(
        verified_state(
            [
                criterion("employee_field", {"employee_id": "E-17", "title": "Senior Engineer"}),
                criterion("employee_field", {"employee_id": "E-17", "salary": 150000}),
            ]
        )
    )
    assert result.status is VerificationStatus.FAILED
    assert [c.passed for c in result.checks] == [True, False]
    assert "salary" in (result.failure_reason or "")


def test_employee_existence_criterion_is_still_supported(verifier: OutcomeVerifier) -> None:
    result = verifier.verify(verified_state([criterion("employee", {"employee_id": "E-17"})]))
    assert result.status is VerificationStatus.PASSED
    assert result.checks[0].name == "employee_exists"


def test_persisted_unknown_field_update_cannot_verify(verifier: OutcomeVerifier) -> None:
    """A successful call recorded before v0.3.0 with an ignored field must not verify."""
    step = update_step({"salary": 150000}).model_copy(update={"status": PlanStepStatus.COMPLETED})
    call = ToolCallRecord(
        task_id="legacy",
        step_id="step_1",
        tool_name="company_api",
        arguments=dict(step.arguments),
        status=ToolCallStatus.SUCCEEDED,
        attempt=1,
    )
    result = verifier.verify(verified_state([], plan=[step], tool_calls=[call]))
    assert result.status is VerificationStatus.FAILED
    unsupported = [c for c in result.checks if c.name == "unsupported_criterion"]
    assert unsupported and "salary" in unsupported[0].message


def test_step_coverage_checks_the_recorded_action(verifier: OutcomeVerifier) -> None:
    step = update_step().model_copy(update={"status": PlanStepStatus.COMPLETED})
    call = ToolCallRecord(
        task_id="t", step_id="step_1", tool_name="company_api",
        arguments=dict(step.arguments), status=ToolCallStatus.SUCCEEDED, attempt=1,
    )
    wrong = criterion("step_completed", {"step_id": "step_1", "tool": "file", "operation": "write"})
    right = criterion(
        "step_completed", {"step_id": "step_1", "tool": "company_api", "operation": "update_employee"}
    )
    state = verified_state([wrong], plan=[step], tool_calls=[call])
    assert verifier.verify(state).checks[0].passed is False
    state = verified_state([right], plan=[step], tool_calls=[call])
    assert verifier.verify(state).checks[0].passed is True


def test_empty_criteria_are_unverified_not_passed(verifier: OutcomeVerifier) -> None:
    result = verifier.verify(verified_state([]))
    assert result.status is VerificationStatus.UNVERIFIED
    assert result.passed is True and result.checks == []
    assert [e.type for e in result.evidence] == ["verification_vacuous"]


def read_only_plan() -> list[PlanStep]:
    return [
        PlanStep(
            step_id="step_1",
            description="Read employee E-17",
            tool_name="company_api",
            arguments={"operation": "get_employee", "employee_id": "E-17"},
        )
    ]


def test_read_only_task_completes_as_unverified(
    runtime: ExecutionRuntime, store: SQLiteStore
) -> None:
    task = runtime.create_task(user_goal="Look up E-17", plan=read_only_plan())
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.UNVERIFIED
    assert "not independently verified" in (final.completion_summary or "")
    types = event_types(store, task.task_id)
    assert EventType.VERIFICATION_UNVERIFIED in types
    assert EventType.VERIFICATION_PASSED not in types
    completed = [e for e in store.get_events(task.task_id) if e.event_type == EventType.TASK_COMPLETED]
    assert completed[0].result["verification"] == "unverified"


# --- Task 3: approvals bound to the exact action -------------------------------------


def test_fingerprint_is_deterministic_and_action_specific() -> None:
    reordered = {"fields": {"title": "Senior Engineer"}, "employee_id": "E-17", "operation": "update_employee"}
    assert action_fingerprint("company_api", UPDATE_ARGS) == action_fingerprint("company_api", reordered)
    assert action_fingerprint("company_api", UPDATE_ARGS).startswith("sha256:")
    variants = [
        action_fingerprint("company_api", {**UPDATE_ARGS, "fields": {"title": "CEO"}}),
        action_fingerprint("company_api", {**UPDATE_ARGS, "employee_id": "E-23"}),
        action_fingerprint("company_api", {**UPDATE_ARGS, "operation": "get_employee"}),
        action_fingerprint("file", UPDATE_ARGS),
    ]
    assert len(set(variants)) == 4
    assert action_fingerprint("company_api", UPDATE_ARGS) not in variants


def paused_task(runtime: ExecutionRuntime, fields: dict | None = None) -> ExecutionState:
    task = runtime.create_task(user_goal="Promote E-17", plan=[update_step(fields)])
    paused = runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    return paused


def mutate_step(store: SQLiteStore, task_id: str, fields: dict, **state_updates: Any) -> None:
    state = store.get_state(task_id)
    plan = [state.plan[0].model_copy(update={"arguments": {**UPDATE_ARGS, "fields": fields}})]
    store.save_state(state.model_copy(update={"plan": plan, **state_updates}))


def test_unchanged_approval_executes_and_records_fingerprint(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    fingerprint = action_fingerprint("company_api", UPDATE_ARGS)
    assert paused.pending_action.fingerprint == fingerprint

    final = runtime.approve_task(paused.task_id, approver="tester", expected_fingerprint=fingerprint)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    events = {e.event_type: e for e in store.get_events(paused.task_id)}
    assert events[EventType.APPROVAL_REQUESTED].result["fingerprint"] == fingerprint
    assert events[EventType.APPROVAL_APPROVED].result["fingerprint"] == fingerprint
    assert events[EventType.APPROVAL_APPROVED].result["approver"] == "tester"
    assert EventType.APPROVAL_INVALIDATED not in events


def test_arguments_changed_while_pending_require_fresh_approval(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    mutate_step(store, paused.task_id, {"title": "CEO"})
    before_events = len(store.get_events(paused.task_id))

    with pytest.raises(StaleApprovalError):
        runtime.approve_task(paused.task_id)

    state = store.get_state(paused.task_id)
    new_fingerprint = action_fingerprint("company_api", {**UPDATE_ARGS, "fields": {"title": "CEO"}})
    assert state.final_status is TaskStatus.NEEDS_APPROVAL
    assert state.approval_status is ApprovalStatus.PENDING
    assert state.pending_action.fingerprint == new_fingerprint
    assert state.pending_action.arguments["fields"] == {"title": "CEO"}
    assert state.tool_calls == []
    assert company_repo.get_employee("E-17").title == "Software Engineer"
    new_events = store.get_events(paused.task_id)[before_events:]
    assert [e.event_type for e in new_events] == [
        EventType.APPROVAL_INVALIDATED,
        EventType.APPROVAL_REQUESTED,
    ]
    invalidated = new_events[0].result
    assert invalidated["previous_fingerprint"] == paused.pending_action.fingerprint
    assert invalidated["current_fingerprint"] == new_fingerprint

    final = runtime.approve_task(paused.task_id, expected_fingerprint=new_fingerprint)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "CEO"


def test_stale_granted_approval_is_never_executed(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    """Approval granted, then the action changed before execution (e.g. crash + edit)."""
    paused = paused_task(runtime)
    mutate_step(
        store,
        paused.task_id,
        {"title": "CEO"},
        approval_status=ApprovalStatus.APPROVED,
        final_status=TaskStatus.RUNNING,
    )

    state = runtime.run(paused.task_id)
    assert state.final_status is TaskStatus.NEEDS_APPROVAL
    assert state.tool_calls == []
    assert state.pending_action.arguments["fields"] == {"title": "CEO"}
    assert company_repo.get_employee("E-17").title == "Software Engineer"
    types = event_types(store, paused.task_id)
    assert EventType.APPROVAL_INVALIDATED in types
    assert EventType.TOOL_CALLED not in types


def test_approval_for_a_different_action_than_reviewed_is_refused(
    runtime: ExecutionRuntime, store: SQLiteStore
) -> None:
    paused = paused_task(runtime)
    before = store.get_state(paused.task_id)
    before_events = len(store.get_events(paused.task_id))
    reviewed = action_fingerprint("company_api", {**UPDATE_ARGS, "fields": {"title": "CEO"}})

    for action in (runtime.approve_task, runtime.reject_task):
        with pytest.raises(StaleApprovalError):
            action(paused.task_id, expected_fingerprint=reviewed)

    assert store.get_state(paused.task_id) == before
    assert len(store.get_events(paused.task_id)) == before_events


def test_tampered_fingerprint_is_treated_as_stale(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    forged = paused.pending_action.model_copy(update={"fingerprint": "sha256:" + "0" * 64})
    store.save_state(paused.model_copy(update={"pending_action": forged}))
    with pytest.raises(StaleApprovalError):
        runtime.approve_task(paused.task_id)
    assert company_repo.get_employee("E-17").title == "Software Engineer"


def test_rejection_records_the_fingerprint(runtime: ExecutionRuntime, store: SQLiteStore) -> None:
    paused = paused_task(runtime)
    final = runtime.reject_task(
        paused.task_id, reason="no", expected_fingerprint=paused.pending_action.fingerprint
    )
    assert final.final_status is TaskStatus.ABORTED
    rejected = [e for e in store.get_events(paused.task_id) if e.event_type == EventType.APPROVAL_REJECTED]
    assert rejected[0].result["fingerprint"] == paused.pending_action.fingerprint


def test_approval_survives_restart(
    db_path: Path, workspace: Path, registry: ToolRegistry, company_repo: CompanyRepository
) -> None:
    first = ExecutionRuntime(store=SQLiteStore(db_path), registry=registry, workspace_path=workspace)
    paused = paused_task(first)

    restarted = ExecutionRuntime(
        store=SQLiteStore(db_path), registry=registry, workspace_path=workspace
    )
    reloaded = restarted.load(paused.task_id)
    assert reloaded.pending_action.fingerprint == paused.pending_action.fingerprint
    final = restarted.approve_task(paused.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"


def legacy(state: ExecutionState, **pending_updates: Any) -> ExecutionState:
    """The same task as persisted by v0.2.0: no fingerprint on the pending action."""
    pending = state.pending_action.model_copy(update={"fingerprint": None, **pending_updates})
    return state.model_copy(update={"pending_action": pending})


def test_legacy_pending_action_with_matching_arguments_is_approvable(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    store.save_state(legacy(paused))
    assert pending_fingerprint(store.get_state(paused.task_id).pending_action) == action_fingerprint(
        "company_api", UPDATE_ARGS
    )

    final = runtime.approve_task(paused.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    approved = [e for e in store.get_events(paused.task_id) if e.event_type == EventType.APPROVAL_APPROVED]
    assert approved[0].result["fingerprint"] == action_fingerprint("company_api", UPDATE_ARGS)


def test_legacy_pending_action_with_different_arguments_is_stale(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    shown = {**UPDATE_ARGS, "fields": {"title": "Intern"}}
    store.save_state(legacy(paused, arguments=shown))
    with pytest.raises(StaleApprovalError):
        runtime.approve_task(paused.task_id)
    assert company_repo.get_employee("E-17").title == "Software Engineer"


def test_legacy_granted_approval_resumes_only_if_unchanged(
    runtime: ExecutionRuntime, store: SQLiteStore, company_repo: CompanyRepository
) -> None:
    paused = paused_task(runtime)
    approved = legacy(paused).model_copy(
        update={"approval_status": ApprovalStatus.APPROVED, "final_status": TaskStatus.RUNNING}
    )
    store.save_state(approved)
    final = runtime.run(paused.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"


def test_action_change_between_retries_requires_fresh_approval(
    store: SQLiteStore,
    registry: ToolRegistry,
    verifier: OutcomeVerifier,
    failure_injector: FailureInjector,
    company_repo: CompanyRepository,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, verifier=verifier, max_recovery_attempts=2)
    paused = paused_task(runtime)
    failure_injector.inject_once(
        "company_api.update_employee", FailureType.TRANSIENT.value, "503 (test)"
    )
    original_load = runtime.load
    calls = {"tool": 0}
    tool = registry.get("company_api")
    original_execute = tool.execute

    def counting_execute(args, *, idempotency_key=None):
        calls["tool"] += 1
        return original_execute(args, idempotency_key=idempotency_key)

    def load_after_failure(task_id: str) -> ExecutionState:
        state = original_load(task_id)
        if calls["tool"] >= 1 and state.final_status is TaskStatus.RUNNING:
            plan = [state.plan[0].model_copy(update={"arguments": {**UPDATE_ARGS, "fields": {"title": "CEO"}}})]
            state = state.model_copy(update={"plan": plan})
        return state

    tool.execute = counting_execute
    runtime.load = load_after_failure
    state = runtime.approve_task(paused.task_id)

    assert calls["tool"] == 1
    assert state.final_status is TaskStatus.NEEDS_APPROVAL
    assert state.pending_action.arguments["fields"] == {"title": "CEO"}
    assert company_repo.get_employee("E-17").title == "Software Engineer"
    assert EventType.APPROVAL_INVALIDATED in event_types(store, paused.task_id)


# --- API: fingerprint exposure and stale-approval handling ----------------------------


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "api" / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "api" / "workspace"),
        allowed_hosts="testserver",
    )


def test_api_binds_approval_to_the_displayed_fingerprint(api_settings: Settings) -> None:
    with TestClient(create_app(api_settings)) as client:
        task_id = wait_for(client, submit(client, EMPLOYEE_GOAL)["task_id"], {"needs_approval"})["task_id"]
        pending = client.get(f"/api/v1/tasks/{task_id}").json()["pending_action"]
        assert pending["fingerprint"] == action_fingerprint("company_api", pending["arguments"])

        url = f"/api/v1/tasks/{task_id}/approve"
        malformed = client.post(url, json={"step_id": "step_1", "confirm": True, "fingerprint": "abc"})
        assert malformed.status_code == 422
        stale = client.post(
            url, json={"step_id": "step_1", "confirm": True, "fingerprint": "sha256:" + "f" * 64}
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_approval"
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"

        approved = client.post(
            url, json={"step_id": "step_1", "confirm": True, "fingerprint": pending["fingerprint"]}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "completed"


def test_api_reports_stale_pending_action_and_new_request(api_settings: Settings) -> None:
    with TestClient(create_app(api_settings)) as client:
        task_id = wait_for(client, submit(client, EMPLOYEE_GOAL)["task_id"], {"needs_approval"})["task_id"]
        store = build_runtime(api_settings).store
        mutate_step(store, task_id, {"title": "CEO"})

        response = client.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_approval"
        pending = client.get(f"/api/v1/tasks/{task_id}").json()["pending_action"]
        assert pending["arguments"]["fields"] == {"title": "CEO"}
        types = [e["event_type"] for e in client.get(f"/api/v1/tasks/{task_id}/history").json()["events"]]
        assert types[-2:] == ["approval.invalidated", "approval.requested"]


def test_api_exposes_unverified_status(api_settings: Settings) -> None:
    rt = build_runtime(api_settings)
    task = rt.runtime.create_task(user_goal="Look up E-17", plan=read_only_plan())
    rt.runtime.run(task.task_id)
    with TestClient(create_app(api_settings)) as client:
        detail = client.get(f"/api/v1/tasks/{task.task_id}").json()
        verification = client.get(f"/api/v1/tasks/{task.task_id}/verification").json()
    assert detail["status"] == "completed"
    assert detail["verification_status"] == "unverified"
    assert verification["status"] == "unverified"
    assert verification["checks_total"] == 0
