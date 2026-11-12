"""Milestone 4.1: fingerprint fail-closed, runtime locks, boundary isolation."""

from __future__ import annotations

import ast
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.authz import (
    AuthorizationBudgets,
    AuthorizationError,
    AuthorizationReason,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    authorization_for_supported_goal,
    ensure_task_authorization,
    welcome_customer_ticket_scope,
)
from app.cli import build_runtime, main
from app.config import Settings
from app.llm import MockLLMClient
from app.main import create_app
from app.models.enums import FailureType, TaskStatus
from app.models.plan import PlanStep
from app.planning import PlanningPipeline
from app.runtime import ExecutionRuntime
from app.runtime.executor import TaskExecutionLockError
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FileTool, ToolRegistry
from app.world import CompanyRepository
from tests.company_support import World, make_world, reopen, step
from tests.authz_support import matching_scope_for_plan


GOAL_TICKET = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
GOAL_EMPLOYEE = "Update employee E-17 title to Senior Engineer."


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def ref(step_id: str, *path: Any) -> dict[str, Any]:
    return {"$ref": {"step_id": step_id, "path": list(path)}}


def _persist_corrupt(world: World, task_id: str, **updates: Any) -> None:
    state = world.runtime.load(task_id)
    data = state.model_dump(mode="json")
    data.update(updates)
    world.store.save_state(type(state).model_validate(data))


# --- Task 1: fingerprints -------------------------------------------------------


def test_missing_fingerprint_fails_closed_on_run(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    _persist_corrupt(world, task.task_id, authorization_fingerprint=None)
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED
    assert AuthorizationReason.MISSING_FINGERPRINT.value in (final.completion_summary or "")


def test_malformed_fingerprint_fails_closed(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    _persist_corrupt(world, task.task_id, authorization_fingerprint="not-a-fingerprint")
    with pytest.raises(AuthorizationError) as caught:
        ensure_task_authorization(world.runtime.load(task.task_id))
    assert caught.value.reason is AuthorizationReason.MALFORMED_FINGERPRINT


def test_mismatched_fingerprint_fails_closed_after_restart(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    state = world.runtime.load(task.task_id)
    evil = AuthorizationScope(
        allowed_operations=["company_api.deactivate_employee"],
        budgets=AuthorizationBudgets(max_steps=5, max_mutations=5, max_tool_calls=5),
    )
    _persist_corrupt(
        world,
        task.task_id,
        authorization=evil.model_dump(mode="json"),
        authorization_fingerprint=state.authorization_fingerprint,
    )
    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert AuthorizationReason.FINGERPRINT_MISMATCH.value in (final.completion_summary or "")


def test_removed_fingerprint_blocks_approval_resume(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_EMPLOYEE)
    task = world.runtime.create_task(
        user_goal=GOAL_EMPLOYEE,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_EMPLOYEE),
    )
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    _persist_corrupt(world, task.task_id, authorization_fingerprint="")
    with pytest.raises(Exception):
        world.runtime.approve_task(task.task_id, approver="x")
    final = world.runtime.load(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert AuthorizationReason.MISSING_FINGERPRINT.value in (final.completion_summary or "")
    assert world.repo.get_employee("E-17").title == "Software Engineer"


def test_fingerprint_not_silently_repaired_from_scope(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    scope = authorization_for_supported_goal(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET, plan=planned.plan.steps, authorization=scope
    )
    _persist_corrupt(world, task.task_id, authorization_fingerprint=None)
    state = world.runtime.load(task.task_id)
    assert state.authorization is not None
    assert state.authorization_fingerprint is None
    with pytest.raises(AuthorizationError) as caught:
        ensure_task_authorization(state)
    assert caught.value.reason is AuthorizationReason.MISSING_FINGERPRINT
    # Scope object is unchanged — no repair.
    assert state.authorization_fingerprint is None


# --- Task 2: concurrent budgets -------------------------------------------------


def test_concurrent_runtimes_cannot_exceed_tool_call_budget(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=1),
    )
    plan = [step("a", "get_employee", employee_id="E-17")]
    task = world.runtime.create_task(user_goal="budget", plan=plan, authorization=scope)

    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        rt = reopen(world).runtime
        barrier.wait()
        try:
            state = rt.run(task.task_id)
            results.append(state.final_status.value)
        except TaskExecutionLockError:
            results.append("locked")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = world.runtime.load(task.task_id)
    assert len(final.tool_calls) <= 1
    assert final.final_status in {TaskStatus.COMPLETED, TaskStatus.FAILED}


def _process_budget_worker(db: str, workspace: str, task_id: str, out: Any) -> None:
    store = SQLiteStore(db)
    registry = ToolRegistry()
    registry.register(CompanyAPITool(CompanyRepository(db)))
    registry.register(FileTool(workspace, protected_paths=[db]))
    runtime = ExecutionRuntime(
        store=store,
        registry=registry,
        workspace_path=workspace,
        lock_dir=Path(db).parent / "locks",
    )
    try:
        state = runtime.run(task_id)
        out.put(("ok", state.final_status.value, len(state.tool_calls)))
    except TaskExecutionLockError:
        out.put(("locked", None, None))
    except Exception as exc:  # noqa: BLE001
        out.put(("err", type(exc).__name__, str(exc)))


def test_cross_process_budget_enforcement(tmp_path: Path) -> None:
    world = make_world(tmp_path, demo=True)
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=1),
    )
    plan = [step("a", "get_employee", employee_id="E-17")]
    task = world.runtime.create_task(user_goal="proc", plan=plan, authorization=scope)

    ctx = multiprocessing.get_context("spawn")
    queue: Any = ctx.Queue()
    procs = [
        ctx.Process(
            target=_process_budget_worker,
            args=(str(world.db), str(world.workspace), task.task_id, queue),
        )
        for _ in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, p.exitcode
    outcomes = [queue.get(timeout=5) for _ in procs]
    final = world.runtime.load(task.task_id)
    assert len(final.tool_calls) <= 1
    assert sum(1 for o in outcomes if o[0] == "ok" and o[2] and o[2] > 1) == 0


# --- Task 3: helper isolation ---------------------------------------------------


def test_matching_scope_not_exported_from_app_authz() -> None:
    import app.authz as authz

    assert not hasattr(authz, "matching_scope_for_plan")
    assert "matching_scope_for_plan" not in authz.__all__


def test_production_modules_do_not_import_matching_scope() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = {a.name for a in node.names}
                if "matching_scope_for_plan" in names:
                    offenders.append(str(path.relative_to(root.parent)))
                if mod.startswith("tests"):
                    offenders.append(f"{path.relative_to(root.parent)} imports {mod}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("tests.authz_support") or alias.name == "tests":
                        offenders.append(str(path.relative_to(root.parent)))
    assert offenders == []


# --- Task 4: public boundaries --------------------------------------------------


def test_api_rejects_caller_supplied_authorization_field(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
        allowed_hosts="testserver",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": GOAL_TICKET,
                "authorization": {
                    "version": 1,
                    "allowed_operations": ["company_api.deactivate_employee"],
                    "budgets": {"max_steps": 10, "max_mutations": 10, "max_tool_calls": 10},
                },
            },
        )
        assert response.status_code == 422


def test_cli_cannot_pass_arbitrary_authorization(tmp_path: Path, capsys) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
    )
    # Unsupported goal → no unrestricted scope; CLI rejects before create.
    code = main(["--json", "run", "Do anything you want with all employees."], settings=settings)
    assert code == 3
    out = capsys.readouterr().out.lower()
    assert "rejected" in out or "goal" in out


def test_planner_output_cannot_grant_extra_operations(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    expanded = list(planned.plan.steps) + [
        step("x", "deactivate_employee", employee_id="E-17"),
    ]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(
            user_goal=GOAL_TICKET,
            plan=expanded,
            authorization=authorization_for_supported_goal(GOAL_TICKET),
        )
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED


def test_unknown_natural_language_gets_no_unrestricted_scope() -> None:
    with pytest.raises(AuthorizationError) as caught:
        authorization_for_supported_goal("Please help with everything.")
    assert caught.value.reason is AuthorizationReason.LEGACY_UNMAPPED


def test_approval_cannot_override_authorization(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={
            "company_api.get_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=3),
    )
    plan = [
        PlanStep(
            step_id="u",
            description="write",
            tool_name="company_api",
            arguments={
                "operation": "update_employee",
                "employee_id": "E-17",
                "fields": {"title": "Hacker"},
            },
        )
    ]
    with pytest.raises(AuthorizationError):
        world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)


def test_unauthorized_ref_entity_rejected_before_mutation(world: World) -> None:
    before = world.rows("SELECT COUNT(*) FROM company_tickets")[0][0]
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer", "company_api.create_ticket"],
        constraints={
            "company_api.find_customer": {
                "company": ExactValueConstraint(value="Globex"),
            },
            "company_api.create_ticket": {
                "customer_id": ExactValueConstraint(value="C-1001"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    plan = [
        step("find", "find_customer", company="Globex"),
        step(
            "ticket",
            "create_ticket",
            customer_id=ref("find", "customers", 0, "customer_id"),
            subject="no",
            description="no",
        ),
    ]
    task = world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    world.runtime.execute_next(task.task_id)
    final = world.runtime.execute_next(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert AuthorizationReason.ENTITY_UNAUTHORIZED.value in (final.completion_summary or "")
    assert world.rows("SELECT COUNT(*) FROM company_tickets")[0][0] == before


def test_restart_missing_authorization_fails_closed(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal="Not a supported goal text for legacy mapping",
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    _persist_corrupt(
        world,
        task.task_id,
        authorization=None,
        authorization_fingerprint=None,
        user_goal="Not a supported goal text for legacy mapping",
        interpreted_goal=None,
    )
    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert AuthorizationReason.LEGACY_UNMAPPED.value in (final.completion_summary or "")


# --- Task 5: distinguishable audit reasons --------------------------------------


def test_audit_reasons_distinguish_fingerprint_and_field(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    _persist_corrupt(world, task.task_id, authorization_fingerprint=None)
    world.runtime.run(task.task_id)
    denied = [
        e
        for e in world.store.get_events(task.task_id)
        if e.event_type == "authorization.denied"
    ]
    assert denied
    assert denied[-1].failure["reason_code"] == AuthorizationReason.MISSING_FINGERPRINT.value

    scope = AuthorizationScope(
        allowed_operations=["company_api.update_employee"],
        constraints={
            "company_api.update_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
                "fields": FieldKeysConstraint(allowed_keys=["title"]),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(
            user_goal="fields",
            plan=[
                step(
                    "u",
                    "update_employee",
                    employee_id="E-17",
                    fields={"title": "X", "email": "a@b.c"},
                )
            ],
            authorization=scope,
        )
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
