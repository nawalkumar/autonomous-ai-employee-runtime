"""Independent checks for the v0.3.0 company operations.

Every check re-reads the persisted company records. Tool results are used only
to locate the before-state a call recorded in the same transaction as its
write, so that unintended changes to other fields are detected.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import ToolCallStatus
from app.models.records import EvidenceItem
from app.models.state import ExecutionState
from app.verify.results import VerificationCheckResult
from app.world.models import ASSIGNABLE_DEPARTMENT
from app.world.repository import CompanyRepository

CheckOutcome = tuple[VerificationCheckResult, list[EvidenceItem]]

PROVENANCE = frozenset({"source_call_id", "source_step_id"})
EVALUABLE_KEYS: dict[str, frozenset[str]] = {
    "ticket_fields": frozenset({"ticket_id", "status", "priority"}) | PROVENANCE,
    "ticket_assignment": frozenset({"ticket_id", "employee_id"}) | PROVENANCE,
    "customer_record": frozenset({"customer_id", "name", "company", "email"}) | PROVENANCE,
    "employee_status": frozenset({"employee_id", "status"}) | PROVENANCE,
}
_TICKET_FIELDS = ("status", "priority", "assignee_id", "customer_id", "subject", "description")
_EMPLOYEE_FIELDS = ("name", "email", "title", "department")


def _fail(name: str, expected: Any, message: str, actual: Any = None) -> VerificationCheckResult:
    return VerificationCheckResult(
        name=name, passed=False, expected=expected, actual=actual, message=message
    )


def _unsupported(expected: Any, reason: str) -> VerificationCheckResult:
    return _fail("unsupported_criterion", expected, f"Unsupported criterion: {reason}")


def _before_state(
    state: ExecutionState, expected: dict[str, Any], entity: str
) -> tuple[dict[str, Any] | None, str | None]:
    """(previous record, error) for a criterion bound to a call; (None, None) if unbound."""
    call_id = expected.get("source_call_id")
    if call_id is None:
        return None, None
    call = next((c for c in state.tool_calls if c.call_id == call_id), None)
    if call is None or call.status is not ToolCallStatus.SUCCEEDED:
        return None, f"source call {call_id} did not succeed in this task"
    data = (call.result or {}).get("data") if isinstance(call.result, dict) else None
    previous = data.get("previous") if isinstance(data, dict) else None
    if not isinstance(previous, dict) or not isinstance(previous.get(entity), (str, type(None))):
        return None, f"source call {call_id} recorded no before-state"
    return previous, None


def _side_effects(previous: dict[str, Any], current: dict[str, Any], fields: tuple[str, ...]) -> dict:
    return {
        f: {"before": previous.get(f), "after": current.get(f)}
        for f in fields
        if previous.get(f) != current.get(f)
    }


def verify_ticket_fields(
    repo: CompanyRepository, state: ExecutionState, expected: dict[str, Any]
) -> CheckOutcome:
    ticket_id = expected.get("ticket_id")
    requested = {k: expected[k] for k in ("status", "priority") if k in expected}
    if not ticket_id or not requested:
        return _unsupported(expected, "ticket_fields criterion needs ticket_id and a field"), []
    ticket = repo.get_ticket(str(ticket_id))
    if ticket is None:
        return _fail("ticket_fields", expected, f"Ticket not found: {ticket_id}"), []
    current = ticket.model_dump(mode="json")
    evidence = [
        EvidenceItem(
            type="ticket_record",
            description=f"Ticket {ticket_id} re-read from company database",
            data=current,
        )
    ]
    mismatched = {k: {"expected": v, "actual": current[k]} for k, v in requested.items() if current[k] != v}
    if mismatched:
        return _fail("ticket_fields", expected, f"Ticket field mismatch: {mismatched}", current), evidence

    previous, error = _before_state(state, expected, "ticket_id")
    if error:
        return _fail("ticket_fields", expected, error, current), evidence
    if previous is not None:
        unchanged = tuple(f for f in _TICKET_FIELDS if f not in requested)
        changed = _side_effects(previous, current, unchanged)
        evidence.append(
            EvidenceItem(
                type="ticket_before_state",
                description=f"Ticket {ticket_id} before the change",
                data=previous,
            )
        )
        if changed:
            return _fail(
                "ticket_fields", expected, f"Unintended ticket changes: {changed}", current
            ), evidence
    return VerificationCheckResult(
        name="ticket_fields",
        passed=True,
        expected=expected,
        actual=current,
        message=f"Ticket {ticket_id} has {', '.join(f'{k}={v}' for k, v in requested.items())}",
    ), evidence


def verify_ticket_assignment(
    repo: CompanyRepository, state: ExecutionState, expected: dict[str, Any]
) -> CheckOutcome:
    ticket_id, employee_id = expected.get("ticket_id"), expected.get("employee_id")
    if not ticket_id or not employee_id:
        return _unsupported(expected, "ticket_assignment criterion needs ticket_id and employee_id"), []
    ticket = repo.get_ticket(str(ticket_id))
    if ticket is None:
        return _fail("ticket_assignment", expected, f"Ticket not found: {ticket_id}"), []
    current = ticket.model_dump(mode="json")
    evidence = [
        EvidenceItem(type="ticket_record", description=f"Ticket {ticket_id} re-read", data=current)
    ]
    if ticket.assignee_id != employee_id:
        return _fail(
            "ticket_assignment", expected,
            f"Ticket {ticket_id} is assigned to {ticket.assignee_id}, not {employee_id}", current,
        ), evidence
    employee = repo.get_employee(str(employee_id))
    if employee is None:
        return _fail("ticket_assignment", expected, f"Assignee not found: {employee_id}", current), evidence
    evidence.append(
        EvidenceItem(
            type="employee_record",
            description=f"Assignee {employee_id} re-read",
            data=employee.model_dump(mode="json"),
        )
    )
    if employee.status != "active" or employee.department != ASSIGNABLE_DEPARTMENT:
        return _fail(
            "ticket_assignment", expected,
            f"Assignee {employee_id} is not an active {ASSIGNABLE_DEPARTMENT} employee "
            f"(status {employee.status}, department {employee.department})",
            current,
        ), evidence

    previous, error = _before_state(state, expected, "ticket_id")
    if error:
        return _fail("ticket_assignment", expected, error, current), evidence
    if previous is not None:
        changed = _side_effects(previous, current, tuple(f for f in _TICKET_FIELDS if f != "assignee_id"))
        if changed:
            return _fail(
                "ticket_assignment", expected, f"Unintended ticket changes: {changed}", current
            ), evidence
    return VerificationCheckResult(
        name="ticket_assignment",
        passed=True,
        expected=expected,
        actual=current,
        message=f"Ticket {ticket_id} is assigned to active {ASSIGNABLE_DEPARTMENT} employee {employee_id}",
    ), evidence


def verify_customer_record(
    repo: CompanyRepository, state: ExecutionState, expected: dict[str, Any]
) -> CheckOutcome:
    customer_id = expected.get("customer_id")
    if not customer_id:
        if "source_call_id" in expected:
            return _fail(
                "customer_record", expected,
                f"create_customer call result has no customer_id (call_id={expected['source_call_id']})",
            ), []
        return _unsupported(expected, "customer_record criterion needs customer_id"), []
    customer = repo.get_customer(str(customer_id))
    if customer is None:
        return _fail("customer_record", expected, f"Customer not found: {customer_id}"), []
    current = customer.model_dump(mode="json")
    evidence = [
        EvidenceItem(type="customer_record", description=f"Customer {customer_id} re-read", data=current)
    ]
    wanted = {
        "name": expected.get("name"),
        "company": expected.get("company"),
        "email": expected.get("email"),
    }
    normalise = {"name": str.strip, "company": str.strip, "email": lambda v: v.strip().lower()}
    mismatched = {
        k: {"expected": v, "actual": current[k]}
        for k, v in wanted.items()
        if v is not None and (not isinstance(v, str) or normalise[k](v) != current[k])
    }
    if mismatched:
        return _fail("customer_record", expected, f"Customer field mismatch: {mismatched}", current), evidence
    same_email = repo.customers_with_email(customer.email)
    if [c.customer_id for c in same_email] != [customer.customer_id]:
        return _fail(
            "customer_record", expected,
            f"Email {customer.email} is shared by {[c.customer_id for c in same_email]}", current,
        ), evidence
    return VerificationCheckResult(
        name="customer_record",
        passed=True,
        expected=expected,
        actual=current,
        message=f"Customer {customer_id} exists with the requested identity and a unique email",
    ), evidence


def verify_employee_status(
    repo: CompanyRepository, state: ExecutionState, expected: dict[str, Any]
) -> CheckOutcome:
    employee_id, status = expected.get("employee_id"), expected.get("status")
    if not employee_id or status not in ("active", "inactive"):
        return _unsupported(expected, "employee_status criterion needs employee_id and status"), []
    employee = repo.get_employee(str(employee_id))
    if employee is None:
        return _fail("employee_status", expected, f"Employee not found: {employee_id}"), []
    current = employee.model_dump(mode="json")
    evidence = [
        EvidenceItem(type="employee_record", description=f"Employee {employee_id} re-read", data=current)
    ]
    if employee.status != status:
        return _fail("employee_status", expected, f"Employee {employee_id} is {employee.status}", current), evidence
    if status == "inactive":
        open_tickets = repo.open_tickets_assigned_to(str(employee_id))
        if open_tickets:
            return _fail(
                "employee_status", expected,
                f"Inactive employee {employee_id} still has open tickets: {open_tickets}", current,
            ), evidence
    previous, error = _before_state(state, expected, "employee_id")
    if error:
        return _fail("employee_status", expected, error, current), evidence
    if previous is not None:
        changed = _side_effects(previous, current, _EMPLOYEE_FIELDS)
        if changed:
            return _fail(
                "employee_status", expected, f"Unintended employee changes: {changed}", current
            ), evidence
    return VerificationCheckResult(
        name="employee_status",
        passed=True,
        expected=expected,
        actual=current,
        message=f"Employee {employee_id} is {status}"
        + (" with no open tickets assigned" if status == "inactive" else ""),
    ), evidence


CHECKS = {
    "ticket_fields": verify_ticket_fields,
    "ticket_assignment": verify_ticket_assignment,
    "customer_record": verify_customer_record,
    "employee_status": verify_employee_status,
}
