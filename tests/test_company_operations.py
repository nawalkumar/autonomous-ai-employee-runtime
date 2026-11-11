"""The five v0.3.0 company operations: contracts, rules, approval, idempotency,
concurrency and fail-closed behaviour (repository, tool and runtime level)."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import pytest

from app.models.enums import FailureType, TaskStatus, VerificationStatus
from app.policy import ApprovalPolicy
from app.runtime import StaleApprovalError
from app.tools import RiskLevel
from app.world.models import TICKET_TRANSITIONS
from tests.company_support import World, make_world, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


@pytest.fixture
def plain(tmp_path: Path) -> World:
    """Default v0.2.0 seed only (3 employees, 3 customers, no tickets)."""
    return make_world(tmp_path, demo=False)


def invoke(world: World, key: str | None = None, **arguments):
    return world.company.invoke(arguments, idempotency_key=key)


def assert_unchanged(world: World, before: dict) -> None:
    assert world.snapshot() == before


# --- list_tickets -----------------------------------------------------------------


@pytest.mark.parametrize(
    "filters,sql,params",
    [
        ({}, "1=1", ()),
        ({"status": "open"}, "status = ?", ("open",)),
        ({"priority": "urgent"}, "priority = ?", ("urgent",)),
        ({"customer_id": "C-1002"}, "customer_id = ?", ("C-1002",)),
        ({"assignee_id": "E-44"}, "assignee_id = ?", ("E-44",)),
        ({"unassigned": True}, "assignee_id IS NULL", ()),
        (
            {"status": "open", "priority": "high", "unassigned": True},
            "status = ? AND priority = ? AND assignee_id IS NULL",
            ("open", "high"),
        ),
    ],
)
def test_list_tickets_matches_an_independent_query(world: World, filters, sql, params) -> None:
    expected = [
        r[0]
        for r in world.rows(
            f"SELECT ticket_id FROM company_tickets WHERE {sql} "
            "ORDER BY CAST(SUBSTR(ticket_id, 3) AS INTEGER)",
            *params,
        )
    ]
    result = invoke(world, operation="list_tickets", filters=filters, limit=100)
    assert result.ok, result.error_message
    assert [t["ticket_id"] for t in result.data["tickets"]] == expected
    assert result.data["total"] == len(expected) == result.data["count"]


def test_list_tickets_pages_are_stable_and_complete(world: World) -> None:
    pages, offset = [], 0
    while True:
        data = invoke(world, operation="list_tickets", limit=7, offset=offset).data
        pages.extend(t["ticket_id"] for t in data["tickets"])
        assert data["total"] == 60
        if data["count"] < 7:
            break
        offset += 7
    assert pages == [f"T-{n}" for n in range(10001, 10061)]
    assert invoke(world, operation="list_tickets").data["count"] == 50  # default page


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "5"},
        {"limit": True},
        {"offset": -1},
        {"filters": {"bogus": 1}},
        {"filters": {"status": "deleted"}},
        {"filters": {"customer_id": "C-1001' OR '1'='1"}},
        {"filters": {"assignee_id": "E-44", "unassigned": True}},
        {"filters": "status = 'open'"},
        {"sql": "SELECT * FROM company_employees"},
        {"where": "1=1"},
    ],
)
def test_list_tickets_rejects_unsafe_or_invalid_queries(world: World, arguments) -> None:
    before = world.snapshot()
    result = invoke(world, operation="list_tickets", **arguments)
    assert not result.ok and result.error_type == FailureType.INVALID_ARGS.value
    assert_unchanged(world, before)


def test_listing_is_read_only_and_reported_unverified(world: World) -> None:
    before = world.snapshot()
    decision = ApprovalPolicy().evaluate(
        tool_name="company_api", arguments={"operation": "list_tickets"}, tool=world.company
    )
    assert (decision.requires_approval, decision.risk) == (False, RiskLevel.READ)
    state = world.run(step("s", "list_tickets", filters={"status": "open"}))
    assert state.final_status is TaskStatus.COMPLETED
    assert state.verification_status is VerificationStatus.UNVERIFIED
    assert state.verification_checks == []
    assert_unchanged(world, before)


# --- update_ticket ------------------------------------------------------------------


STATUSES = sorted(TICKET_TRANSITIONS)


@pytest.mark.parametrize("source,target", list(itertools.product(STATUSES, STATUSES)))
def test_status_transitions_follow_the_table(plain: World, source: str, target: str) -> None:
    ticket = plain.repo.create_ticket(customer_id="C-1001", subject="s", description="d")
    plain.execute("UPDATE company_tickets SET status = ? WHERE ticket_id = ?", source, ticket.ticket_id)
    before = plain.snapshot()
    result = invoke(plain, operation="update_ticket", ticket_id=ticket.ticket_id, fields={"status": target})
    allowed = target == source or target in TICKET_TRANSITIONS[source]
    assert result.ok is allowed, result.error_message
    if allowed:
        assert plain.repo.get_ticket(ticket.ticket_id).status == target
        assert result.data["previous"]["status"] == source
    else:
        assert result.error_type == FailureType.INVALID_ARGS.value
        assert f"from {source} to {target}" in result.error_message
        assert_unchanged(plain, before)


def test_closed_tickets_are_terminal_and_closing_needs_resolution() -> None:
    assert TICKET_TRANSITIONS["closed"] == frozenset()
    assert "closed" not in TICKET_TRANSITIONS["open"] | TICKET_TRANSITIONS["in_progress"]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"subject": "changed"},
        {"assignee_id": "E-40"},
        {"status": "resolved", "subject": "changed"},
        {"priority": "critical"},
        {"status": None},
    ],
)
def test_update_ticket_rejects_unsupported_fields_atomically(world: World, fields) -> None:
    before = world.snapshot()
    result = invoke(world, "k-1", operation="update_ticket", ticket_id="T-10005", fields=fields)
    assert not result.ok and result.error_type == FailureType.INVALID_ARGS.value
    assert_unchanged(world, before)


def test_priority_update_and_idempotent_replay(world: World) -> None:
    first = invoke(world, "k-1", operation="update_ticket", ticket_id="T-10006", fields={"priority": "high"})
    second = invoke(world, "k-1", operation="update_ticket", ticket_id="T-10006", fields={"priority": "high"})
    assert first.ok and second.ok
    assert second.data["idempotent_replay"] is True
    assert second.data["previous"]["priority"] == "low"
    assert world.rows("SELECT COUNT(*) FROM company_idempotency WHERE idempotency_key='k-1'") == [(1,)]
    conflict = invoke(world, "k-1", operation="update_ticket", ticket_id="T-10006", fields={"priority": "urgent"})
    assert conflict.error_type == FailureType.INVALID_ARGS.value
    assert world.repo.get_ticket("T-10006").priority == "high"


def test_only_closing_requires_approval() -> None:
    policy = ApprovalPolicy()

    def needs(fields):
        return policy.evaluate(
            tool_name="company_api",
            arguments={"operation": "update_ticket", "ticket_id": "T-1", "fields": fields},
        ).requires_approval

    assert needs({"status": "closed"}) is True
    assert needs({"status": "closed", "priority": "low"}) is True
    assert needs({"status": "resolved"}) is False
    assert needs({"priority": "urgent"}) is False


@pytest.mark.parametrize("ticket_id,error", [("T-99999", "environment_unexpected"), ("10001", "invalid_args")])
def test_update_ticket_unknown_or_malformed_ids(world: World, ticket_id: str, error: str) -> None:
    result = invoke(world, operation="update_ticket", ticket_id=ticket_id, fields={"priority": "low"})
    assert not result.ok and result.error_type == error


# --- assign_ticket ------------------------------------------------------------------


def test_assign_to_active_support_employee(world: World) -> None:
    result = invoke(world, "k", operation="assign_ticket", ticket_id="T-10001", employee_id="E-40")
    assert result.ok
    assert world.repo.get_ticket("T-10001").assignee_id == "E-40"
    assert result.data["previous"]["assignee_id"] is None
    reassigned = invoke(world, "k2", operation="assign_ticket", ticket_id="T-10001", employee_id="E-41")
    assert reassigned.ok and reassigned.data["previous"]["assignee_id"] == "E-40"


@pytest.mark.parametrize(
    "ticket_id,employee_id,error,message",
    [
        ("T-10001", "E-45", "policy_blocked", "inactive"),
        ("T-10001", "E-17", "policy_blocked", "Engineering"),
        ("T-10009", "E-40", "policy_blocked", "resolved"),
        ("T-10018", "E-40", "policy_blocked", "closed"),
        ("T-10001", "E-999", "environment_unexpected", "not found"),
        ("T-99999", "E-40", "environment_unexpected", "not found"),
        ("T-10001", "40", "invalid_args", "employee_id"),
    ],
)
def test_assignment_rules(world: World, ticket_id, employee_id, error, message) -> None:
    before = world.snapshot()
    result = invoke(world, "k", operation="assign_ticket", ticket_id=ticket_id, employee_id=employee_id)
    assert not result.ok and result.error_type == error
    assert message in result.error_message
    assert_unchanged(world, before)


# --- create_customer ------------------------------------------------------------------


def test_create_customer_allocates_the_next_identifier(plain: World) -> None:
    result = invoke(plain, "k", operation="create_customer", name=" Pat Lee ", company="Nimbus Labs", email="Pat@Nimbus.Example")
    assert result.ok
    assert result.data["customer"] == {
        **result.data["customer"],
        "customer_id": "C-1004",
        "name": "Pat Lee",
        "email": "pat@nimbus.example",
    }
    second = invoke(plain, "k2", operation="create_customer", name="Ana Ruiz", company="Vela", email="ana@vela.example")
    assert second.data["customer"]["customer_id"] == "C-1005"


def test_create_customer_on_demo_data_continues_after_c_1010(world: World) -> None:
    result = invoke(world, "k", operation="create_customer", name="N", company="Co", email="n@co.example")
    assert result.data["customer"]["customer_id"] == "C-1011"


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"name": "X", "company": "Acme", "email": "BILLING@acme.example"}, "policy_blocked"),
        ({"name": "X", "company": "Co", "email": "not-an-email"}, "invalid_args"),
        ({"name": " ", "company": "Co", "email": "a@b.example"}, "invalid_args"),
        ({"name": "X", "company": "Co"}, "invalid_args"),
        ({"name": "X" * 201, "company": "Co", "email": "a@b.example"}, "invalid_args"),
        ({"name": "X", "company": "Co", "email": "a@b.example", "customer_id": "C-1"}, "invalid_args"),
    ],
)
def test_create_customer_rejects_invalid_or_duplicate(plain: World, arguments, error) -> None:
    before = plain.snapshot()
    result = invoke(plain, "k", operation="create_customer", **arguments)
    assert not result.ok and result.error_type == error
    assert_unchanged(plain, before)


def test_create_customer_requires_approval_and_retries_never_duplicate(plain: World) -> None:
    decision = ApprovalPolicy().evaluate(
        tool_name="company_api", arguments={"operation": "create_customer"}, tool=plain.company
    )
    assert decision.requires_approval and decision.risk is RiskLevel.WRITE
    plain.injector.inject_once("company_api.create_customer", FailureType.TRANSIENT.value, "503 (test)")
    state = plain.run(step("c", "create_customer", name="Pat Lee", company="Nimbus", email="pat@nimbus.example"))
    assert state.final_status is TaskStatus.COMPLETED and state.retry_count == 1
    assert plain.rows("SELECT customer_id FROM company_customers WHERE email = 'pat@nimbus.example'") == [("C-1004",)]
    replay = invoke(
        plain, f"{state.task_id}:c", operation="create_customer",
        name="Pat Lee", company="Nimbus", email="pat@nimbus.example",
    )
    assert replay.data["idempotent_replay"] is True and replay.data["customer"]["customer_id"] == "C-1004"
    assert plain.rows("SELECT COUNT(*) FROM company_customers") == [(4,)]


def test_concurrent_customer_creation_allocates_unique_ids(plain: World) -> None:
    barrier = threading.Barrier(8)
    results = []

    def create(i: int) -> None:
        barrier.wait()
        results.append(
            invoke(plain, f"k-{i}", operation="create_customer", name=f"N{i}", company="Co", email=f"n{i}@co.example")
        )

    threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r.ok for r in results)
    ids = sorted(r.data["customer"]["customer_id"] for r in results)
    assert ids == [f"C-{n}" for n in range(1004, 1012)]


def test_concurrent_duplicate_email_creates_one_customer(plain: World) -> None:
    barrier = threading.Barrier(6)
    results = []

    def create(i: int) -> None:
        barrier.wait()
        results.append(
            invoke(plain, f"k-{i}", operation="create_customer", name="Dup", company="Co", email="dup@co.example")
        )

    threads = [threading.Thread(target=create, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(r.ok for r in results) == [False] * 5 + [True]
    assert {r.error_type for r in results if not r.ok} == {"policy_blocked"}
    assert plain.rows("SELECT COUNT(*) FROM company_customers WHERE email = 'dup@co.example'") == [(1,)]


# --- deactivate_employee --------------------------------------------------------------


def test_deactivation_is_refused_while_open_tickets_are_assigned(world: World) -> None:
    before = world.snapshot()
    result = invoke(world, "k", operation="deactivate_employee", employee_id="E-44")
    assert not result.ok and result.error_type == FailureType.POLICY_BLOCKED.value
    for ticket_id in ("T-10013", "T-10014", "T-10015"):
        assert ticket_id in result.error_message
    assert "reassign" in result.error_message
    assert_unchanged(world, before)


def test_deactivation_after_reassignment_and_repeat_is_idempotent(world: World) -> None:
    for ticket_id in ("T-10013", "T-10014", "T-10015"):
        assert invoke(world, f"a-{ticket_id}", operation="assign_ticket", ticket_id=ticket_id, employee_id="E-40").ok
    result = invoke(world, "k", operation="deactivate_employee", employee_id="E-44")
    assert result.ok and result.data["employee"]["status"] == "inactive"
    assert result.data["previous"]["status"] == "active"
    again = invoke(world, "k-other", operation="deactivate_employee", employee_id="E-44")
    assert again.ok and again.data["previous"]["status"] == "inactive"
    replay = invoke(world, "k", operation="deactivate_employee", employee_id="E-44")
    assert replay.data["idempotent_replay"] is True
    blocked = invoke(world, "a2", operation="assign_ticket", ticket_id="T-10001", employee_id="E-44")
    assert blocked.error_type == FailureType.POLICY_BLOCKED.value


def test_deactivation_needs_approval_and_is_destructive(world: World) -> None:
    decision = ApprovalPolicy().evaluate(
        tool_name="company_api", arguments={"operation": "deactivate_employee", "employee_id": "E-70"},
        tool=world.company,
    )
    assert decision.requires_approval and decision.risk is RiskLevel.DESTRUCTIVE
    state = world.run(step("d", "deactivate_employee", employee_id="E-70"), approve=False)
    assert state.final_status is TaskStatus.NEEDS_APPROVAL
    assert world.repo.get_employee("E-70").status == "active"
    rejected = world.runtime.reject_task(state.task_id, reason="no")
    assert rejected.final_status is TaskStatus.ABORTED
    assert world.repo.get_employee("E-70").status == "active"


@pytest.mark.parametrize("employee_id,error", [("E-999", "environment_unexpected"), ("bob", "invalid_args")])
def test_deactivation_unknown_or_malformed_employee(world: World, employee_id, error) -> None:
    result = invoke(world, operation="deactivate_employee", employee_id=employee_id)
    assert not result.ok and result.error_type == error


# --- Runtime: approval binding, stale approvals and retries for new operations ----------


def test_stale_approval_for_a_new_operation_is_never_executed(world: World) -> None:
    paused = world.run(step("c", "update_ticket", ticket_id="T-10009", fields={"status": "closed"}), approve=False)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action.fingerprint
    state = world.store.get_state(paused.task_id)
    plan = [state.plan[0].model_copy(update={"arguments": {**state.plan[0].arguments, "ticket_id": "T-10010"}})]
    world.store.save_state(state.model_copy(update={"plan": plan}))
    with pytest.raises(StaleApprovalError):
        world.runtime.approve_task(paused.task_id)
    assert world.repo.get_ticket("T-10009").status == "resolved"
    assert world.repo.get_ticket("T-10010").status == "resolved"
    assert world.store.get_state(paused.task_id).pending_action.arguments["ticket_id"] == "T-10010"


def test_transient_failure_retry_writes_once(world: World) -> None:
    world.injector.inject_once("company_api.update_ticket", FailureType.TRANSIENT.value, "503 (test)")
    state = world.run(step("u", "update_ticket", ticket_id="T-10005", fields={"priority": "urgent"}))
    assert state.final_status is TaskStatus.COMPLETED and state.retry_count == 1
    assert state.verification_status is VerificationStatus.PASSED
    assert world.rows(
        "SELECT COUNT(*) FROM company_idempotency WHERE operation = 'update_ticket'"
    ) == [(1,)]


def test_policy_blocked_failures_are_not_retried(world: World) -> None:
    state = world.run(step("d", "deactivate_employee", employee_id="E-44"))
    assert state.final_status is TaskStatus.FAILED
    assert state.retry_count == 0
    assert [c.error_type for c in state.tool_calls] == [FailureType.POLICY_BLOCKED.value]
    assert world.repo.get_employee("E-44").status == "active"


@pytest.mark.parametrize(
    "goal",
    [
        "Deactivate employee E-44.",
        "Close ticket T-10009.",
        "Create a customer named Pat Lee at Nimbus Labs.",
        "Assign ticket T-10001 to E-40.",
    ],
)
def test_new_operations_are_unreachable_over_http(tmp_path: Path, goal: str) -> None:
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db = tmp_path / "api" / "data" / "runtime.db"
    db.parent.mkdir(parents=True)
    from app.world.demo_fixture import seed_demo_company

    seed_demo_company(db)
    world = make_world(tmp_path / "api", demo=False)
    before = world.snapshot()
    settings = Settings(
        _env_file=None,
        database_path=str(db),
        workspace_path=str(tmp_path / "api" / "workspace"),
        allowed_hosts="testserver",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/tasks", json={"goal": goal})
        assert response.status_code == 422
        employees = client.get("/api/v1/company/employees").json()["employees"]
        tickets = client.get("/api/v1/company/tickets").json()["tickets"]
    assert world.snapshot() == before
    assert {e["employee_id"]: e["status"] for e in employees}["E-45"] == "inactive"
    assert {t["ticket_id"]: t["assignee_id"] for t in tickets}["T-10013"] == "E-44"


def test_unregistered_company_operations_still_fail_closed(world: World) -> None:
    for operation in ("delete_ticket", "reactivate_employee", "merge_tickets"):
        result = invoke(world, operation=operation, ticket_id="T-10001")
        assert result.error_type == FailureType.INVALID_ARGS.value
        assert world.company.is_retry_safe({"operation": operation}) is False
