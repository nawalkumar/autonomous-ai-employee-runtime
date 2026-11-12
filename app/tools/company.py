"""Company API tool backed by the mock company world."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import FailureType
from app.tools.base import BaseTool, RiskLevel
from app.tools.capabilities import CAPABILITIES, operation_of
from app.tools.failures import FailureInjector
from app.tools.result import ToolResult
from app.world.models import (
    CUSTOMER_ID_PATTERN,
    EMPLOYEE_ID_PATTERN,
    TICKET_ID_PATTERN,
    TicketFilters,
    TicketPriority,
)
from app.world.repository import (
    MAX_TICKET_PAGE,
    BusinessRuleError,
    CompanyRepository,
    IdempotencyConflictError,
    InvalidCustomerError,
    InvalidEmployeeUpdateError,
    RecordNotFoundError,
    TicketUpdateError,
    validate_employee_update,
    validate_new_customer,
    validate_ticket_update,
)

CompanyOperation = Literal[
    "get_employee",
    "update_employee",
    "find_customer",
    "create_ticket",
    "get_ticket",
    "list_tickets",
    "update_ticket",
    "assign_ticket",
    "create_customer",
    "deactivate_employee",
]

# Operations added in v0.3.0 accept only their declared inputs and validate
# identifier formats. The original five keep their v0.2.0 argument handling.
STRICT_OPERATIONS = frozenset(
    {"list_tickets", "update_ticket", "assign_ticket", "create_customer", "deactivate_employee"}
)
MAX_TICKET_OFFSET = 100_000
DEFAULT_TICKET_PAGE = 50
_ID_PATTERNS = {
    "ticket_id": re.compile(TICKET_ID_PATTERN),
    "employee_id": re.compile(EMPLOYEE_ID_PATTERN),
    "customer_id": re.compile(CUSTOMER_ID_PATTERN),
}


def _write_result(
    entity: str,
    data: dict[str, Any],
    evidence_type: str,
    replayed: bool,
    idempotency_key: str | None,
    *,
    previous: dict[str, Any] | None = None,
) -> ToolResult:
    """Normalized write result; a replay returns the originally committed record.

    ``previous`` is the record as it was before the change, captured in the
    same transaction; the verifier uses it to detect unintended side effects.
    """
    extra = {"previous": previous} if previous is not None else {}
    if replayed:
        return ToolResult.success(
            {entity: data, **extra, "idempotent_replay": True},
            evidence=[
                {
                    "type": "idempotent_replay",
                    "data": {"idempotency_key": idempotency_key, entity: data},
                }
            ],
        )
    return ToolResult.success(
        {entity: data, **extra},
        evidence=[{"type": evidence_type, "data": {entity: data, **extra} if extra else data}],
    )


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
    # Inputs of the v0.3.0 operations. Untyped here so that the original five
    # operations accept exactly what they accepted before; they are validated in
    # _validate_strict and typed precisely in the capability input schemas.
    name: Any = None
    filters: Any = None
    limit: Any = None
    offset: Any = None

    @model_validator(mode="before")
    @classmethod
    def _strict_inputs(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("operation") not in STRICT_OPERATIONS:
            return data
        allowed = set(CAPABILITIES.require("company_api", data["operation"]).input_fields)
        unexpected = sorted(str(key) for key in data if key != "operation" and key not in allowed)
        if unexpected:
            raise ValueError(
                f"Unsupported argument(s) for {data['operation']}: {', '.join(unexpected)}"
            )
        for key, pattern in _ID_PATTERNS.items():
            if key in data and data[key] is not None and not (
                isinstance(data[key], str) and pattern.fullmatch(data[key])
            ):
                raise ValueError(f"Invalid {key}: {data[key]!r}")
        return data

    @model_validator(mode="after")
    def validate_operation_args(self) -> "CompanyAPIArgs":
        op = self.operation
        if op in STRICT_OPERATIONS:
            self._validate_strict(op)
            return self
        if op == "get_employee" and not self.employee_id:
            raise ValueError("employee_id is required for get_employee")
        if op == "update_employee":
            if not self.employee_id:
                raise ValueError("employee_id is required for update_employee")
            if not self.fields:
                raise ValueError("fields is required for update_employee")
            validate_employee_update(self.fields)
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

    def _validate_strict(self, op: str) -> None:
        required = CAPABILITIES.require("company_api", op).required_inputs
        missing = [name for name in required if getattr(self, name) in (None, "", {})]
        if missing:
            raise ValueError(f"{', '.join(missing)} required for {op}")
        if op == "list_tickets":
            TicketFilters.model_validate(self.filters or {})
            for key, low, high in (("limit", 1, MAX_TICKET_PAGE), ("offset", 0, MAX_TICKET_OFFSET)):
                value = getattr(self, key)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
                ):
                    raise ValueError(f"{key} must be an integer from {low} to {high}")
        elif op == "update_ticket":
            validate_ticket_update(self.fields)
        elif op == "create_customer":
            validate_new_customer(self.name, self.company, self.email)


class CompanyAPITool(BaseTool):
    name = "company_api"
    description = (
        "Access the mock company system: employees, customers, and support tickets."
    )
    args_schema = CompanyAPIArgs
    risk_level = RiskLevel.WRITE
    retry_safe = True

    def __init__(
        self,
        repository: CompanyRepository,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.repository = repository
        self.failure_injector = failure_injector or FailureInjector()

    def risk_for(self, operation: CompanyOperation) -> RiskLevel:
        """Raises UnknownCapabilityError for an unregistered operation."""
        return CAPABILITIES.require(self.name, operation).risk

    def is_retry_safe(self, args: BaseModel | dict[str, Any] | None = None) -> bool:
        capability = CAPABILITIES.find(self.name, operation_of(args))
        return capability is not None and capability.retry_safe

    def metadata(self) -> dict[str, Any]:
        meta = super().metadata()
        capabilities = CAPABILITIES.for_tool(self.name)
        meta["operations"] = {c.operation: c.risk.value for c in capabilities}
        meta["retry_safe_operations"] = sorted(c.operation for c in capabilities if c.retry_safe)
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

        if args.operation == "update_employee":
            return self._update_employee(args, idempotency_key)
        if args.operation == "create_ticket":
            return self._create_ticket(args, idempotency_key)

        if args.operation in STRICT_OPERATIONS:
            return self._execute_strict(args, idempotency_key)

        handlers = {
            "get_employee": self._get_employee,
            "find_customer": self._find_customer,
            "get_ticket": self._get_ticket,
        }
        return handlers[args.operation](args)

    def _execute_strict(self, args: CompanyAPIArgs, idempotency_key: str | None) -> ToolResult:
        handlers = {
            "list_tickets": lambda: self._list_tickets(args),
            "update_ticket": lambda: self._update_ticket(args, idempotency_key),
            "assign_ticket": lambda: self._assign_ticket(args, idempotency_key),
            "create_customer": lambda: self._create_customer(args, idempotency_key),
            "deactivate_employee": lambda: self._deactivate_employee(args, idempotency_key),
        }
        try:
            return handlers[args.operation]()
        except (TicketUpdateError, InvalidCustomerError, IdempotencyConflictError) as exc:
            return ToolResult.failure(FailureType.INVALID_ARGS.value, str(exc))
        except BusinessRuleError as exc:
            return ToolResult.failure(FailureType.POLICY_BLOCKED.value, str(exc))
        except RecordNotFoundError as exc:
            return ToolResult.failure(FailureType.ENVIRONMENT_UNEXPECTED.value, str(exc))

    def _list_tickets(self, args: CompanyAPIArgs) -> ToolResult:
        filters = TicketFilters.model_validate(args.filters or {})
        limit = args.limit if args.limit is not None else DEFAULT_TICKET_PAGE
        offset = args.offset or 0
        tickets, total = self.repository.query_tickets(
            **filters.model_dump(), limit=limit, offset=offset
        )
        payload = [t.model_dump(mode="json") for t in tickets]
        applied = filters.model_dump(exclude_defaults=True)
        return ToolResult.success(
            {
                "tickets": payload,
                "count": len(payload),
                "total": total,
                "limit": limit,
                "offset": offset,
                "filters": applied,
            },
            evidence=[
                {
                    "type": "ticket_listing",
                    "data": {"filters": applied, "total": total, "count": len(payload)},
                }
            ],
        )

    def _update_ticket(self, args: CompanyAPIArgs, idempotency_key: str | None) -> ToolResult:
        ticket, previous, replayed = self.repository.update_ticket_idempotent(
            args.ticket_id or "", args.fields, idempotency_key=idempotency_key
        )
        if ticket is None or previous is None:
            raise RecordNotFoundError(f"Ticket not found: {args.ticket_id}")
        return _write_result(
            "ticket", ticket.model_dump(mode="json"), "ticket_updated", replayed,
            idempotency_key, previous=previous.model_dump(mode="json"),
        )

    def _assign_ticket(self, args: CompanyAPIArgs, idempotency_key: str | None) -> ToolResult:
        ticket, previous, replayed = self.repository.assign_ticket_idempotent(
            args.ticket_id or "", args.employee_id or "", idempotency_key=idempotency_key
        )
        return _write_result(
            "ticket", ticket.model_dump(mode="json"), "ticket_assigned", replayed,
            idempotency_key, previous=previous.model_dump(mode="json"),
        )

    def _create_customer(self, args: CompanyAPIArgs, idempotency_key: str | None) -> ToolResult:
        customer, replayed = self.repository.create_customer_idempotent(
            name=args.name, company=args.company, email=args.email,
            idempotency_key=idempotency_key,
        )
        return _write_result(
            "customer", customer.model_dump(mode="json"), "customer_created", replayed,
            idempotency_key,
        )

    def _deactivate_employee(
        self, args: CompanyAPIArgs, idempotency_key: str | None
    ) -> ToolResult:
        employee, previous, replayed = self.repository.deactivate_employee_idempotent(
            args.employee_id or "", idempotency_key=idempotency_key
        )
        if employee is None or previous is None:
            raise RecordNotFoundError(f"Employee not found: {args.employee_id}")
        return _write_result(
            "employee", employee.model_dump(mode="json"), "employee_deactivated", replayed,
            idempotency_key, previous=previous.model_dump(mode="json"),
        )

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

    def _update_employee(
        self, args: CompanyAPIArgs, idempotency_key: str | None
    ) -> ToolResult:
        try:
            updated, replayed = self.repository.update_employee_idempotent(
                args.employee_id or "", args.fields, idempotency_key=idempotency_key
            )
        except (IdempotencyConflictError, InvalidEmployeeUpdateError) as exc:
            return ToolResult.failure(FailureType.INVALID_ARGS.value, str(exc))
        if updated is None:
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Employee not found: {args.employee_id}",
            )
        data = updated.model_dump(mode="json")
        return _write_result("employee", data, "employee_updated", replayed, idempotency_key)

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

    def _create_ticket(
        self, args: CompanyAPIArgs, idempotency_key: str | None
    ) -> ToolResult:
        try:
            ticket, replayed = self.repository.create_ticket_idempotent(
                customer_id=args.customer_id or "",
                subject=args.subject or "",
                description=args.description or "",
                priority=args.priority,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return ToolResult.failure(
                FailureType.INVALID_ARGS.value,
                str(exc),
            )
        data = ticket.model_dump(mode="json")
        return _write_result("ticket", data, "ticket_created", replayed, idempotency_key)

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
