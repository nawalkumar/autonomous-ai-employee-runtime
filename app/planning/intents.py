"""Trusted goal-intent catalog (deterministic; independent of the LLM).

When dynamic planning is enabled, natural-language goals must match one of these
grammars (or the original two mock scenarios) before any LLM call. Entity IDs
and bounded fields are taken from the matched text — never inferred by the
model. Ambiguous or unmatched goals fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from app.models.goal import InterpretedGoal, SuccessCriterion

# ID fragments for case-insensitive fullmatch against soft-normalized goals.
_CID = r"c-[0-9]{1,9}"
_EID = r"e-[0-9]{1,9}"
_TID = r"t-[0-9]{1,9}"

IntentId = Literal[
    "list_open_high_priority_tickets",
    "list_tickets_for_customer",
    "create_ticket_for_customer",
    "update_ticket_status",
    "assign_ticket",
    "update_employee_title",
    "create_customer_and_welcome_ticket",
]

SUPPORTED_DYNAMIC_INTENTS: tuple[str, ...] = (
    "List open high-priority tickets.",
    "List / retrieve / get tickets for customer C-<id>.",
    "Create a support ticket for customer C-<id> about <subject>.",
    "Mark ticket T-<id> as <status> "
    "(or: Update ticket T-<id> status to <status>).",
    "Assign ticket T-<id> to [eligible Support] employee E-<id>.",
    "Update employee E-<id> title to <title>.",
    "Create a new customer named <name> at company <company> "
    "with email <email> and a welcome ticket.",
)

_TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
_STATUS_ALT = "|".join(_TICKET_STATUSES)
_SUBJECT = r"[a-z0-9][a-z0-9 $.,'_-]{2,79}"
_TITLE = r"[a-z0-9][a-z0-9 .,'-]{1,63}"
_PERSON = r"[a-z0-9][a-z0-9 .'-]{0,62}"
_EMAIL = r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}"

_LIST_OPEN_HIGH = re.compile(
    r"(?:list|show) open high(?:-|\s)priority tickets"
    r"|list open tickets with(?: a)? priority(?: of)? high"
)
_LIST_FOR_CUSTOMER = re.compile(
    rf"(?:list|retrieve|get|show) tickets for customer (?P<customer_id>{_CID})"
)
_CREATE_TICKET = re.compile(
    rf"create a support ticket for customer (?P<customer_id>{_CID}) "
    rf"about (?P<subject>{_SUBJECT})"
)
_UPDATE_STATUS = re.compile(
    rf"(?:mark|set) ticket (?P<ticket_id>{_TID}) as (?P<status>{_STATUS_ALT})"
    rf"|update ticket (?P<ticket_id2>{_TID})(?:'s)? status "
    rf"to (?P<status2>{_STATUS_ALT})"
)
_ASSIGN = re.compile(
    rf"assign ticket (?P<ticket_id>{_TID}) to "
    rf"(?:(?:an? )?eligible support employee )?"
    rf"(?P<employee_id>{_EID})"
)
_UPDATE_TITLE = re.compile(
    rf"update employee (?P<employee_id>{_EID})(?:'s)? "
    rf"title to (?P<title>{_TITLE})"
)
_CREATE_CUSTOMER = re.compile(
    rf"create a new customer named (?P<name>{_PERSON}) "
    rf"at company (?P<company>{_PERSON}) "
    rf"with email (?P<email>{_EMAIL}) "
    rf"and a welcome(?: support)? ticket"
)


@dataclass(frozen=True)
class TrustedIntent:
    """Deterministic interpretation of a supported dynamic business goal."""

    intent_id: IntentId
    original_request: str
    objective: str
    entities: dict[str, Any]
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[SuccessCriterion, ...] = ()
    required_information: tuple[str, ...] = ()

    def to_interpreted_goal(self) -> InterpretedGoal:
        return InterpretedGoal(
            original_request=self.original_request,
            objective=self.objective,
            entities=dict(self.entities),
            constraints=list(self.constraints),
            success_criteria=list(self.success_criteria),
            required_information=list(self.required_information),
        )


def _soft_normalize(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip()
    if normalized.endswith("."):
        normalized = normalized[:-1].rstrip()
    return normalized


def parse_trusted_intent(text: str) -> TrustedIntent | None:
    """Return a trusted intent when the whole request matches a catalog grammar."""
    original = text.strip()
    if not original:
        return None
    soft = _soft_normalize(original)
    lowered = soft.lower()

    if _LIST_OPEN_HIGH.fullmatch(lowered):
        return TrustedIntent(
            intent_id="list_open_high_priority_tickets",
            original_request=original,
            objective="List open high-priority tickets.",
            entities={"status": "open", "priority": "high"},
            constraints=("read_only",),
            # Verification is derived from executed operations (read-only → unverified).
        )

    match = _LIST_FOR_CUSTOMER.fullmatch(lowered)
    if match:
        customer_id = match.group("customer_id").upper()
        return TrustedIntent(
            intent_id="list_tickets_for_customer",
            original_request=original,
            objective=f"List tickets for customer {customer_id}.",
            entities={"customer_id": customer_id},
            constraints=("read_only", f"customer_id={customer_id}"),
        )

    match = _CREATE_TICKET.fullmatch(lowered)
    if match:
        customer_id = match.group("customer_id").upper()
        # Preserve subject casing from the soft-normalized original.
        subject_match = _CREATE_TICKET.fullmatch(soft)
        subject = (subject_match.group("subject") if subject_match else match.group("subject")).strip()
        return TrustedIntent(
            intent_id="create_ticket_for_customer",
            original_request=original,
            objective=f"Create a support ticket for customer {customer_id}.",
            entities={
                "customer_id": customer_id,
                "subject": subject,
                "description": subject,
            },
            constraints=(f"customer_id={customer_id}",),
            # ticket_exists / ticket_record criteria are derived from the create_ticket call.
        )

    match = _UPDATE_STATUS.fullmatch(lowered)
    if match:
        ticket_id = (match.group("ticket_id") or match.group("ticket_id2") or "").upper()
        status = (match.group("status") or match.group("status2") or "")
        assert status in _TICKET_STATUSES
        return TrustedIntent(
            intent_id="update_ticket_status",
            original_request=original,
            objective=f"Update ticket {ticket_id} status to {status}.",
            entities={"ticket_id": ticket_id, "status": status},
            constraints=(f"ticket_id={ticket_id}", f"status={status}"),
        )

    match = _ASSIGN.fullmatch(lowered)
    if match:
        ticket_id = match.group("ticket_id").upper()
        employee_id = match.group("employee_id").upper()
        return TrustedIntent(
            intent_id="assign_ticket",
            original_request=original,
            objective=f"Assign ticket {ticket_id} to employee {employee_id}.",
            entities={"ticket_id": ticket_id, "employee_id": employee_id},
            constraints=(f"ticket_id={ticket_id}", f"employee_id={employee_id}"),
        )

    match = _UPDATE_TITLE.fullmatch(lowered)
    if match:
        employee_id = match.group("employee_id").upper()
        title_match = _UPDATE_TITLE.fullmatch(soft)
        display_title = (title_match.group("title") if title_match else match.group("title")).strip()
        return TrustedIntent(
            intent_id="update_employee_title",
            original_request=original,
            objective=f"Update employee {employee_id} title to {display_title}.",
            entities={"employee_id": employee_id, "title": display_title},
            constraints=(f"employee_id={employee_id}", "fields=title"),
            success_criteria=(
                SuccessCriterion(
                    description=f"Employee {employee_id} has title {display_title}",
                    criterion_type="employee_field",
                    target="employees",
                    expected_value={"employee_id": employee_id, "title": display_title},
                ),
            ),
        )

    match = _CREATE_CUSTOMER.fullmatch(lowered)
    if match:
        cased = _CREATE_CUSTOMER.fullmatch(soft)
        name = (cased.group("name") if cased else match.group("name")).strip()
        company = (cased.group("company") if cased else match.group("company")).strip()
        email = (cased.group("email") if cased else match.group("email")).strip().lower()
        return TrustedIntent(
            intent_id="create_customer_and_welcome_ticket",
            original_request=original,
            objective=(
                f"Create customer {name} at {company} and a welcome support ticket."
            ),
            entities={
                "name": name,
                "company": company,
                "email": email,
                "create_step_id": "create_customer",
                "subject": "Welcome",
                "description": f"Welcome ticket for {name} at {company}.",
            },
            constraints=(
                "create_customer then create_ticket",
                "ticket.customer_id from create_customer output",
            ),
            required_information=("customer_id from create_customer output",),
        )

    return None


def describe_trusted_intent(intent: TrustedIntent) -> str:
    return f"intent:{intent.intent_id}"
