"""Mock company domain entities."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


TicketStatus = Literal["open", "in_progress", "resolved", "closed"]
TicketPriority = Literal["low", "medium", "high", "urgent"]


class Employee(BaseModel):
    employee_id: str
    name: str
    email: str
    title: str
    department: str
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
