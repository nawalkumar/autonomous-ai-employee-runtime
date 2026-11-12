"""Mock company domain entities."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


TicketStatus = Literal["open", "in_progress", "resolved", "closed"]
TicketPriority = Literal["low", "medium", "high", "urgent"]
EmployeeStatus = Literal["active", "inactive"]

# Allowed ticket status changes. A ticket is closed only after it was resolved;
# closed is terminal. Setting the current status again is a no-op, not a transition.
TICKET_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_progress", "resolved"}),
    "in_progress": frozenset({"open", "resolved"}),
    "resolved": frozenset({"in_progress", "closed"}),
    "closed": frozenset(),
}
# Tickets that still need work: they block deactivating their assignee and are
# the only tickets that can be (re)assigned.
OPEN_TICKET_STATUSES = frozenset({"open", "in_progress"})
# Only active employees of this department can be assigned tickets.
ASSIGNABLE_DEPARTMENT = "Support"

TICKET_ID_PATTERN = r"^T-[0-9]{1,9}$"
EMPLOYEE_ID_PATTERN = r"^E-[0-9]{1,9}$"
CUSTOMER_ID_PATTERN = r"^C-[0-9]{1,9}$"


class TicketFilters(BaseModel):
    """Equality filters for listing tickets; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    customer_id: str | None = Field(default=None, pattern=CUSTOMER_ID_PATTERN)
    assignee_id: str | None = Field(default=None, pattern=EMPLOYEE_ID_PATTERN)
    unassigned: bool = False

    @model_validator(mode="after")
    def _exclusive_assignee(self) -> "TicketFilters":
        if self.unassigned and self.assignee_id:
            raise ValueError("assignee_id and unassigned cannot be combined")
        return self


class Employee(BaseModel):
    employee_id: str
    name: str
    email: str
    title: str
    department: str
    # Absent before v0.3.0; every pre-existing employee is active.
    status: EmployeeStatus = "active"
    updated_at: datetime = Field(default_factory=utc_now)


class Customer(BaseModel):
    customer_id: str
    name: str
    email: str
    company: str
    created_at: datetime = Field(default_factory=utc_now)


class Ticket(BaseModel):
    ticket_id: str
    customer_id: str
    subject: str
    description: str
    status: TicketStatus = "open"
    priority: TicketPriority = "medium"
    # Absent before v0.3.0; no pre-existing ticket is assigned.
    assignee_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
