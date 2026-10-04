"""Company API tool backed by the mock company world."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import FailureType
from app.tools.base import BaseTool, RiskLevel
from app.tools.failures import FailureInjector
from app.tools.result import ToolResult
from app.world.models import TicketPriority
from app.world.repository import CompanyRepository

CompanyOperation = Literal[
    "get_employee",
    "update_employee",
    "find_customer",
    "create_ticket",
    "get_ticket",
]

_WRITE_OPS = frozenset({"update_employee", "create_ticket"})


class CompanyAPIArgs(BaseModel):
    operation: CompanyOperation
    employee_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    customer_id: str | None = None
    company: str | None = None
    email: str | None = None
    subject: str | None = None
    description: str | None = None
    priority: TicketPriority = "medium"
    ticket_id: str | None = None

    @model_validator(mode="after")
    def validate_operation_args(self) -> "CompanyAPIArgs":
        op = self.operation
        if op == "get_employee" and not self.employee_id:
            raise ValueError("employee_id is required for get_employee")
        if op == "update_employee":
            if not self.employee_id:
                raise ValueError("employee_id is required for update_employee")
            if not self.fields:
                raise ValueError("fields is required for update_employee")
        if op == "find_customer" and not any(
            [self.customer_id, self.company, self.email]
        ):
            raise ValueError(
                "find_customer requires customer_id, company, or email"
            )
        if op == "create_ticket":
            if not self.customer_id:
                raise ValueError("customer_id is required for create_ticket")
            if not self.subject:
                raise ValueError("subject is required for create_ticket")
            if not self.description:
                raise ValueError("description is required for create_ticket")
        if op == "get_ticket" and not self.ticket_id:
            raise ValueError("ticket_id is required for get_ticket")
        return self


class CompanyAPITool(BaseTool):
    name = "company_api"
    description = (
        "Access the mock company system: employees, customers, and support tickets."
    )
    args_schema = CompanyAPIArgs
    risk_level = RiskLevel.WRITE

    def __init__(
        self,
        repository: CompanyRepository,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.repository = repository
        self.failure_injector = failure_injector or FailureInjector()

    def risk_for(self, operation: CompanyOperation) -> RiskLevel:
        return RiskLevel.WRITE if operation in _WRITE_OPS else RiskLevel.READ

    def metadata(self) -> dict[str, Any]:
        meta = super().metadata()
        meta["operations"] = {
            "get_employee": RiskLevel.READ.value,
            "update_employee": RiskLevel.WRITE.value,
            "find_customer": RiskLevel.READ.value,
            "create_ticket": RiskLevel.WRITE.value,
            "get_ticket": RiskLevel.READ.value,
        }
        return meta

    def execute(
        self,
        args: BaseModel,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        assert isinstance(args, CompanyAPIArgs)
        operation_key = f"{self.name}.{args.operation}"
        injected = self.failure_injector.consume(operation_key)
        if injected is not None:
            # Failure happens before any mutation.
            return injected

        _ = idempotency_key  # Interface groundwork only in Phase 2.

        handlers = {
            "get_employee": self._get_employee,
            "update_employee": self._update_employee,
            "find_customer": self._find_customer,
            "create_ticket": self._create_ticket,
            "get_ticket": self._get_ticket,
        }
        return handlers[args.operation](args)

    def _get_employee(self, args: CompanyAPIArgs) -> ToolResult:
        employee = self.repository.get_employee(args.employee_id or "")
        if employee is None:
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Employee not found: {args.employee_id}",
            )
        data = employee.model_dump(mode="json")
        return ToolResult.success(
            {"employee": data},
            evidence=[{"type": "employee_record", "data": data}],
        )

    def _update_employee(self, args: CompanyAPIArgs) -> ToolResult:
        updated = self.repository.update_employee(args.employee_id or "", args.fields)
        if updated is None:
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Employee not found: {args.employee_id}",
            )
        data = updated.model_dump(mode="json")
        return ToolResult.success(
            {"employee": data},
            evidence=[{"type": "employee_updated", "data": data}],
        )

    def _find_customer(self, args: CompanyAPIArgs) -> ToolResult:
        customers = self.repository.find_customers(
            customer_id=args.customer_id,
            company=args.company,
            email=args.email,
        )
        payload = [c.model_dump(mode="json") for c in customers]
        return ToolResult.success(
            {"customers": payload, "count": len(payload)},
            evidence=[{"type": "customer_search", "data": {"count": len(payload)}}],
        )

    def _create_ticket(self, args: CompanyAPIArgs) -> ToolResult:
        try:
            ticket = self.repository.create_ticket(
                customer_id=args.customer_id or "",
                subject=args.subject or "",
                description=args.description or "",
                priority=args.priority,
            )
        except ValueError as exc:
            return ToolResult.failure(
                FailureType.INVALID_ARGS.value,
                str(exc),
            )
        data = ticket.model_dump(mode="json")
        return ToolResult.success(
            {"ticket": data},
            evidence=[{"type": "ticket_created", "data": data}],
        )

    def _get_ticket(self, args: CompanyAPIArgs) -> ToolResult:
        ticket = self.repository.get_ticket(args.ticket_id or "")
        if ticket is None:
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Ticket not found: {args.ticket_id}",
            )
        data = ticket.model_dump(mode="json")
        return ToolResult.success(
            {"ticket": data},
            evidence=[{"type": "ticket_record", "data": data}],
        )
