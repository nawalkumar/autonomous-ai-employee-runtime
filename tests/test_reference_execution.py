"""Step-output references at execution time (milestone 3): resolution,
approval binding, crash recovery, idempotency and independent verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models.enums import FailureType, TaskStatus, ToolCallStatus, VerificationStatus
from app.models.references import contains_reference
from app.policy import ApprovalPolicy
from app.runtime import EventType, StaleApprovalError, action_fingerprint
from tests.company_support import (
    SimulatedCrash,
    World,
    checks,
    crash_during,
    make_world,
    reopen,
    step,
)


def ref(step_id: str, *path: Any) -> dict[str, Any]:
    return {"$ref": {"step_id": step_id, "path": list(path)}}


def welcome_plan(email: str = "iris@lumen.example"):
    return [
        step("create_customer", "create_customer", name="Iris Novak", company="Lumen Studios", email=email),
        step(
            "welcome", "create_ticket",
            customer_id=ref("create_customer", "customer", "customer_id"),
            subject="Welcome to CentrAlign", description="Onboarding call and account setup.",
            priority="low",
        ),
    ]


def acme_plan():
    """Producer needs no approval, so execute_next can stop between the steps."""
    return [
        step("find", "find_customer", company="Acme Corp"),
        step(
            "ticket", "create_ticket", customer_id=ref("find", "customers", 0, "customer_id"),
            subject="Follow-up", description="Billing follow-up.",
        ),
    ]


@pytest.fixture(params=["demo", "fresh"])
def world(request, tmp_path: Path) -> World:
    return make_world(tmp_path, demo=request.param == "demo")


def counts(world: World) -> tuple[int, int]:
    return (
        world.rows("SELECT COUNT(*) FROM company_customers")[0][0],
        world.rows("SELECT COUNT(*) FROM company_tickets")[0][0],
    )


def events(world: World, task_id: str) -> list[str]:
    return [e.event_type for e in world.store.get_events(task_id)]


# --- Acceptance: customer -> welcome ticket ---------------------------------------------


def test_create_customer_then_welcome_ticket_for_that_customer(world: World) -> None:
    customers_before, tickets_before = counts(world)
    expected_customer = world.rows("SELECT next_value FROM company_ticket_seq WHERE name = 'customer'")[0][0]
    expected_ticket = world.rows("SELECT next_value FROM company_ticket_seq WHERE name = 'ticket'")[0][0]
    plan = welcome_plan()
    assert "C-" not in str(plan[1].arguments)  # nothing hard-coded

    task = world.runtime.create_task(user_goal="Onboard Lumen Studios", plan=plan)
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action.step_id == "create_customer"
    assert counts(world) == (customers_before, tickets_before)

    final = world.runtime.approve_task(task.task_id, approver="sales-lead")
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert checks(final) == {
        "step_completed": True, "customer_record": True, "reference_binding": True, "ticket_exists": True,
    }

    customer_id = f"C-{expected_customer}"
    ticket_id = f"T-{expected_ticket}"
    assert world.rows("SELECT name, company, email FROM company_customers WHERE customer_id = ?", customer_id) == [
        ("Iris Novak", "Lumen Studios", "iris@lumen.example")
    ]
    assert world.rows("SELECT customer_id, subject, priority FROM company_tickets WHERE ticket_id = ?", ticket_id) == [
        (customer_id, "Welcome to CentrAlign", "low")
    ]
    assert counts(world) == (customers_before + 1, tickets_before + 1)

    stored = world.store.get_state(task.task_id)
    assert stored.plan[1].arguments["customer_id"] == ref("create_customer", "customer", "customer_id")
    welcome = [c for c in stored.tool_calls if c.step_id == "welcome"]
    assert len(welcome) == 1 and welcome[0].arguments["customer_id"] == customer_id
    assert welcome[0].references == [
        {"location": ["customer_id"], "step_id": "create_customer", "path": ["customer", "customer_id"]}
    ]
    resolved = [e for e in world.store.get_events(task.task_id) if e.event_type == EventType.ARGUMENTS_RESOLVED]
    assert resolved[0].arguments["customer_id"] == customer_id


# --- Approval and fingerprints -----------------------------------------------------------


def test_conditional_approval_is_evaluated_on_resolved_arguments(world: World) -> None:
    world.repo.create_ticket(customer_id="C-1001", subject="source", description="d")  # fresh DBs need tickets
    source = world.rows("SELECT ticket_id FROM company_tickets ORDER BY ticket_id DESC LIMIT 1")[0][0]
    world.execute("UPDATE company_tickets SET status = 'closed' WHERE ticket_id = ?", source)
    target = world.repo.create_ticket(customer_id="C-1001", subject="target", description="d").ticket_id
    world.execute("UPDATE company_tickets SET status = 'resolved' WHERE ticket_id = ?", target)
    consumer = step("close", "update_ticket", ticket_id=target, fields={"status": ref("source", "ticket", "status")})
    assert not ApprovalPolicy().evaluate(tool_name="company_api", arguments=consumer.arguments).requires_approval

    task = world.runtime.create_task(user_goal="g", plan=[step("source", "get_ticket", ticket_id=source), consumer])
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    pending = paused.pending_action
    assert pending.arguments == {"operation": "update_ticket", "ticket_id": target, "fields": {"status": "closed"}}
    assert pending.fingerprint == action_fingerprint("company_api", pending.arguments)
    assert not contains_reference(pending.arguments)
    requested = [e for e in world.store.get_events(task.task_id) if e.event_type == EventType.APPROVAL_REQUESTED]
    assert requested[0].arguments == pending.arguments
    assert world.repo.get_ticket(target).status == "resolved"

    final = world.runtime.approve_task(task.task_id, expected_fingerprint=pending.fingerprint)
    assert final.verification_status is VerificationStatus.PASSED
    assert world.repo.get_ticket(target).status == "closed"


def test_resolved_values_that_need_no_approval_run_directly(world: World) -> None:
    a = world.repo.create_ticket(customer_id="C-1001", subject="a", description="d").ticket_id
    b = world.repo.create_ticket(customer_id="C-1001", subject="b", description="d").ticket_id
    world.execute("UPDATE company_tickets SET status = 'in_progress' WHERE ticket_id = ?", a)
    final = world.run(
        step("source", "get_ticket", ticket_id=a),
        step("move", "update_ticket", ticket_id=b, fields={"status": ref("source", "ticket", "status")}),
        approve=False,
    )
    assert final.final_status is TaskStatus.COMPLETED
    assert world.repo.get_ticket(b).status == "in_progress"


def deactivation_plan(employee_id: str = "E-23"):
    return [
        step("lookup", "get_employee", employee_id=employee_id),
        step("deactivate", "deactivate_employee", employee_id=ref("lookup", "employee", "employee_id")),
    ]


def test_stale_approval_after_producer_output_changes(world: World) -> None:
    task = world.runtime.create_task(user_goal="offboard", plan=deactivation_plan("E-23"))
    paused = world.runtime.run(task.task_id)
    assert paused.pending_action.arguments["employee_id"] == "E-23"

    state = world.store.get_state(task.task_id)
    call = next(c for c in state.tool_calls if c.step_id == "lookup")
    tampered = call.model_copy(update={"result": {**call.result, "data": {"employee": {**call.result["data"]["employee"], "employee_id": "E-17"}}}})
    world.store.save_state(state.model_copy(update={"tool_calls": [tampered if c.call_id == call.call_id else c for c in state.tool_calls]}))

    with pytest.raises(StaleApprovalError):
        world.runtime.approve_task(task.task_id, expected_fingerprint=None)
    renewed = world.store.get_state(task.task_id)
    assert renewed.pending_action.arguments["employee_id"] == "E-17"
    assert EventType.APPROVAL_INVALIDATED in events(world, task.task_id)
    assert world.repo.get_employee("E-23").status == "active"
    assert world.repo.get_employee("E-17").status == "active"

    final = world.runtime.approve_task(task.task_id)
    assert world.repo.get_employee("E-17").status == "inactive"
    assert world.repo.get_employee("E-23").status == "active"
    # The binding check compares with the (tampered) persisted producer output,
    # and the employee check re-reads the database: both agree on E-17.
    assert final.final_status is TaskStatus.COMPLETED


def test_restart_while_awaiting_approval(world: World) -> None:
    task = world.runtime.create_task(user_goal="offboard", plan=deactivation_plan("E-23"))
    world.runtime.run(task.task_id)
    restarted = reopen(world)
    final = restarted.runtime.approve_task(task.task_id, approver="hr")
    assert final.final_status is TaskStatus.COMPLETED
    assert checks(final) == {"step_completed": True, "reference_binding": True, "employee_status": True}
    assert restarted.repo.get_employee("E-23").status == "inactive"


def test_verifier_requires_approval_for_the_exact_action(world: World) -> None:
    task = world.runtime.create_task(user_goal="offboard", plan=deactivation_plan("E-23"))
    world.runtime.run(task.task_id)
    final = world.runtime.approve_task(task.task_id)
    call = next(c for c in final.tool_calls if c.step_id == "deactivate")
    assert call.approval_required is True
    assert call.approval_fingerprint == action_fingerprint("company_api", call.arguments)

    forged = call.model_copy(update={"approval_fingerprint": "sha256:" + "0" * 64})
    state = final.model_copy(update={"tool_calls": [forged if c.call_id == call.call_id else c for c in final.tool_calls]})
    result = world.runtime.verifier.verify(state)
    assert result.status is VerificationStatus.FAILED
    assert "without an approval bound to its exact action" in (result.failure_reason or "")


# --- Runtime safety -----------------------------------------------------------------------


def test_failed_producer_stops_the_plan(world: World) -> None:
    world.repo.create_customer_idempotent(name="X", company="Y", email="iris@lumen.example")
    before = counts(world)
    final = world.run(*welcome_plan())
    assert final.final_status is TaskStatus.FAILED
    assert [c.step_id for c in final.tool_calls] == ["create_customer"]
    assert counts(world) == before
    assert final.plan[1].status.value == "pending"


def test_missing_persisted_output_fails_without_side_effects(world: World) -> None:
    task = world.runtime.create_task(user_goal="g", plan=acme_plan())
    state = world.runtime.execute_next(task.task_id)  # find completes
    assert state.plan[0].status.value == "completed"
    call = state.tool_calls[0]
    world.store.save_state(state.model_copy(update={"tool_calls": [call.model_copy(update={"result": {"ok": True}})]}))
    before = counts(world)
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.MISSING_INFO
    assert "no persisted output" in final.failures[-1].message
    assert EventType.ARGUMENTS_UNRESOLVED in events(world, task.task_id)
    assert [c.step_id for c in final.tool_calls] == ["find"]
    assert counts(world) == before


@pytest.mark.parametrize(
    "path,failure",
    [(["customers", 5, "customer_id"], FailureType.MISSING_INFO), (["customers", 0, "phone"], FailureType.MISSING_INFO)],
)
def test_missing_output_fields_fail_with_typed_errors(world: World, path, failure) -> None:
    plan = acme_plan()
    plan[1] = plan[1].model_copy(update={"arguments": {**plan[1].arguments, "customer_id": ref("find", *path)}})
    task = world.runtime.create_task(user_goal="g", plan=plan)
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is failure
    assert counts(world)[1] == world.rows("SELECT COUNT(*) FROM company_tickets")[0][0]
    assert [c.step_id for c in final.tool_calls] == ["find"]


def test_resolved_value_of_the_wrong_type_is_rejected_before_execution(world: World) -> None:
    """The plan-time check is deferred for free-form outputs; the runtime check is not."""
    plan = [
        step("listing", "list_tickets", filters={"customer_id": "C-1001"}),
        # `count` is an int; ticket_id requires a string — reject before invoke.
        step("bad", "assign_ticket", ticket_id=ref("listing", "count"), employee_id="E-31"),
    ]
    task = world.runtime.create_task(user_goal="g", plan=plan)
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.INVALID_ARGS
    assert "Resolved arguments are invalid" in final.failures[-1].message
    assert "bad" not in [c.step_id for c in final.tool_calls]
    assert EventType.ARGUMENTS_UNRESOLVED in events(world, task.task_id)


def test_interrupted_producer_is_resumed_once(world: World) -> None:
    before = counts(world)
    task = world.runtime.create_task(user_goal="g", plan=welcome_plan())
    world.runtime.run(task.task_id)
    crash_during(world, "create_customer", after_commit=True)
    with pytest.raises(SimulatedCrash):
        world.runtime.approve_task(task.task_id)
    assert counts(world) == (before[0] + 1, before[1])

    restarted = reopen(world)
    final = restarted.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert counts(world) == (before[0] + 1, before[1] + 1)
    producer_calls = [c for c in final.tool_calls if c.step_id == "create_customer"]
    assert [c.status for c in producer_calls] == [ToolCallStatus.FAILED, ToolCallStatus.SUCCEEDED]
    assert producer_calls[1].result["data"]["idempotent_replay"] is True
    assert final.verification_status is VerificationStatus.PASSED


def test_restart_after_producer_success_does_not_repeat_it(world: World) -> None:
    task = world.runtime.create_task(user_goal="g", plan=welcome_plan())
    world.runtime.run(task.task_id)
    crash_during(world, "create_ticket", after_commit=False)
    with pytest.raises(SimulatedCrash):
        world.runtime.approve_task(task.task_id)
    customers, tickets = counts(world)

    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert counts(world) == (customers, tickets + 1)
    assert len([c for c in final.tool_calls if c.step_id == "create_customer"]) == 1
    welcome = [c for c in final.tool_calls if c.step_id == "welcome"]
    assert len({c.arguments["customer_id"] for c in welcome}) == 1
    assert {c.idempotency_key for c in welcome} == {f"{task.task_id}:welcome"}


def test_restart_during_consumer_after_commit_replays(world: World) -> None:
    task = world.runtime.create_task(user_goal="g", plan=welcome_plan())
    world.runtime.run(task.task_id)
    crash_during(world, "create_ticket", after_commit=True)
    with pytest.raises(SimulatedCrash):
        world.runtime.approve_task(task.task_id)
    customers, tickets = counts(world)

    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert counts(world) == (customers, tickets)
    welcome = [c for c in final.tool_calls if c.step_id == "welcome"]
    assert welcome[-1].result["data"]["idempotent_replay"] is True
    assert final.verification_status is VerificationStatus.PASSED


def test_consumer_retries_with_the_same_resolved_arguments(world: World) -> None:
    world.injector.inject_once("company_api.create_ticket", FailureType.TRANSIENT.value, "503 (test)")
    customers, tickets = counts(world)
    final = world.run(*welcome_plan())
    assert final.final_status is TaskStatus.COMPLETED and final.retry_count == 1
    welcome = [c for c in final.tool_calls if c.step_id == "welcome"]
    assert len(welcome) == 2
    assert welcome[0].arguments == welcome[1].arguments
    assert counts(world) == (customers + 1, tickets + 1)


@pytest.mark.parametrize("after_commit", [False, True])
def test_changed_producer_output_never_reuses_the_idempotency_key(world: World, after_commit: bool) -> None:
    task = world.runtime.create_task(user_goal="g", plan=acme_plan())
    world.runtime.execute_next(task.task_id)  # find completes; ticket still pending
    crash_during(world, "create_ticket", after_commit=after_commit)
    with pytest.raises(SimulatedCrash):
        world.runtime.run(task.task_id)
    _, tickets = counts(world)

    state = world.store.get_state(task.task_id)
    find = state.tool_calls[0]
    data = {**find.result["data"], "customers": [{**find.result["data"]["customers"][0], "customer_id": "C-1002"}]}
    world.store.save_state(state.model_copy(update={"tool_calls": [find.model_copy(update={"result": {**find.result, "data": data}}), *state.tool_calls[1:]]}))

    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert EventType.ACTION_CHANGED in events(world, task.task_id)
    assert "refusing to reuse idempotency key" in final.failures[-1].message
    assert counts(world)[1] == tickets
    assert world.rows("SELECT COUNT(*) FROM company_tickets WHERE customer_id = 'C-1002' AND subject = 'Follow-up'") == [(0,)]


# --- Verification ----------------------------------------------------------------------------


def completed_welcome(world: World):
    final = world.run(*welcome_plan())
    assert final.verification_status is VerificationStatus.PASSED
    return final


def failed_messages(world: World, state) -> str:
    result = world.runtime.verifier.verify(world.runtime.load(state.task_id))
    assert result.status is VerificationStatus.FAILED
    return " | ".join(c.message for c in result.checks if not c.passed)


def test_ticket_for_another_customer_fails_verification(world: World) -> None:
    final = completed_welcome(world)
    ticket_id = next(c for c in final.tool_calls if c.step_id == "welcome").result["data"]["ticket"]["ticket_id"]
    world.execute("UPDATE company_tickets SET customer_id = 'C-1001' WHERE ticket_id = ?", ticket_id)
    assert "customer mismatch" in failed_messages(world, final)


def test_deleted_records_fail_verification(world: World) -> None:
    final = completed_welcome(world)
    customer_id = next(c for c in final.tool_calls if c.step_id == "create_customer").result["data"]["customer"]["customer_id"]
    world.execute("PRAGMA foreign_keys = OFF")
    world.execute("DELETE FROM company_tickets WHERE customer_id = ?", customer_id)
    assert "not found" in failed_messages(world, final)


def test_used_value_differing_from_produced_value_fails(world: World) -> None:
    final = completed_welcome(world)
    state = world.store.get_state(final.task_id)
    tampered = [
        c.model_copy(update={"arguments": {**c.arguments, "customer_id": "C-1002"}}) if c.step_id == "welcome" else c
        for c in state.tool_calls
    ]
    result = world.runtime.verifier.verify(state.model_copy(update={"tool_calls": tampered}))
    assert result.status is VerificationStatus.FAILED
    binding = next(c for c in result.checks if c.name == "reference_binding")
    assert not binding.passed and "C-1002" in binding.message


def test_unsupported_binding_criterion_fails(world: World) -> None:
    from app.models.goal import SuccessCriterion

    task = world.runtime.create_task(
        user_goal="g",
        plan=[step("r", "get_employee", employee_id="E-17")],
        success_criteria=[SuccessCriterion(description="x", criterion_type="reference_binding", expected_value={"location": ["x"], "path": ["y"], "extra": 1})],
    )
    final = world.runtime.run(task.task_id)
    assert final.verification_status is VerificationStatus.FAILED
    assert final.verification_checks[0]["name"] == "unsupported_criterion"


def test_read_only_plan_with_references_stays_unverified(world: World) -> None:
    final = world.run(
        step("find", "find_customer", company="Acme Corp"),
        step("again", "find_customer", customer_id=ref("find", "customers", 0, "customer_id")),
    )
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.UNVERIFIED
    assert final.tool_calls[-1].arguments["customer_id"] == "C-1001"


def test_literal_steps_record_no_references(world: World) -> None:
    final = world.run(step("t", "create_ticket", customer_id="C-1001", subject="s", description="d"))
    assert final.tool_calls[0].references is None
    assert final.tool_calls[0].approval_required is False
    assert EventType.ARGUMENTS_RESOLVED not in events(world, final.task_id)
