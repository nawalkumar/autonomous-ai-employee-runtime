"""Operation-level capability registry: the single source of operation metadata.

Each registered tool operation has one immutable Capability describing its
inputs, outputs, risk, approval rule, retry safety, idempotency strategy,
preconditions, verification strategy and the state it may change.

Approval, risk, retry safety and the "mutating operation" set are read from
here by the approval policy, the tools and the verifier's rules. The verifier
still performs the actual independent checks; a capability only names which
strategy applies. Operations that are not registered have no capability and
therefore no permissions: lookups fail instead of falling back to defaults.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from app.tools.base import RiskLevel
from app.world.models import Customer, Employee, Ticket
from app.world.repository import EMPLOYEE_UPDATE_FIELDS


class IdempotencyStrategy(str, Enum):
    # Reads have no side effect to repeat.
    NOT_REQUIRED = "not_required"
    # The write and a (key, request hash, response) record commit in one SQLite
    # transaction; the key is task_id:step_id, so a retry replays the result.
    KEYED_TRANSACTION = "keyed_transaction"
    # Writing the same content to the same path again yields the same state.
    DETERMINISTIC_OVERWRITE = "deterministic_overwrite"


class VerificationStrategy(str, Enum):
    # Nothing to verify: the operation does not change state.
    NONE = "none"
    # Employee fields re-read from the company database.
    EMPLOYEE_FIELDS = "employee_fields"
    # The ticket created by this call re-read from the company database.
    TICKET_RECORD = "ticket_record"
    # The file re-read from the workspace, including expected content.
    FILE_CONTENT = "file_content"


class StateScope(str, Enum):
    NONE = "none"
    COMPANY = "company"
    WORKSPACE = "workspace"


# --- Output contracts (validated against real tool results in tests) ---------------


class EmployeeOutput(BaseModel):
    employee: Employee


class EmployeeWriteOutput(EmployeeOutput):
    idempotent_replay: bool | None = None


class CustomerSearchOutput(BaseModel):
    customers: list[Customer]
    count: int


class TicketOutput(BaseModel):
    ticket: Ticket


class TicketWriteOutput(TicketOutput):
    idempotent_replay: bool | None = None


class FileReadOutput(BaseModel):
    path: str
    content: str


class FileWriteOutput(BaseModel):
    path: str
    bytes_written: int


class FileListOutput(BaseModel):
    files: list[str]
    count: int


@dataclass(frozen=True)
class Capability:
    tool: str
    operation: str
    description: str
    risk: RiskLevel
    requires_approval: bool
    retry_safe: bool
    idempotency: IdempotencyStrategy
    mutates: StateScope
    verification: VerificationStrategy
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    # At least one of these inputs must be provided.
    any_of_inputs: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    approval_reason: str | None = None
    output_model: type[BaseModel] | None = None
    # JSON-schema refinements for input properties the args model types loosely.
    input_constraints: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def key(self) -> tuple[str, str]:
        return (self.tool, self.operation)

    @property
    def mutating(self) -> bool:
        return self.mutates is not StateScope.NONE

    @property
    def input_fields(self) -> tuple[str, ...]:
        return self.required_inputs + self.optional_inputs + self.any_of_inputs

    def input_schema(self, args_model: type[BaseModel]) -> dict[str, Any]:
        """JSON schema for this operation, derived from the tool's args model."""
        source = args_model.model_json_schema().get("properties", {})
        properties: dict[str, Any] = {"operation": {"const": self.operation, "type": "string"}}
        for name in self.input_fields:
            properties[name] = {**source[name], **dict(self.input_constraints.get(name, {}))}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": ["operation", *self.required_inputs],
        }
        if self.any_of_inputs:
            schema["anyOf"] = [{"required": [name]} for name in self.any_of_inputs]
        return schema

    def output_schema(self) -> dict[str, Any] | None:
        return self.output_model.model_json_schema() if self.output_model else None


class UnknownCapabilityError(LookupError):
    """The tool operation is not registered; it has no permissions."""


def operation_of(args: BaseModel | Mapping[str, Any] | None) -> Any:
    """The ``operation`` of tool arguments given as a mapping or an args model."""
    if args is None:
        return None
    if isinstance(args, Mapping):
        return args.get("operation")
    return getattr(args, "operation", None)


class CapabilityRegistry:
    """Immutable lookup of capabilities by (tool, operation)."""

    def __init__(self, capabilities: tuple[Capability, ...]) -> None:
        by_key: dict[tuple[str, str], Capability] = {}
        for capability in capabilities:
            if capability.key in by_key:
                raise ValueError(f"Duplicate capability: {capability.tool}.{capability.operation}")
            by_key[capability.key] = capability
        self._ordered = tuple(capabilities)
        self._by_key = MappingProxyType(by_key)

    def __iter__(self) -> Iterator[Capability]:
        return iter(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def find(self, tool: str, operation: Any) -> Capability | None:
        if not isinstance(operation, str):
            return None
        return self._by_key.get((tool, operation))

    def require(self, tool: str, operation: Any) -> Capability:
        capability = self.find(tool, operation)
        if capability is None:
            raise UnknownCapabilityError(f"Unregistered operation: {tool}.{operation}")
        return capability

    def for_tool(self, tool: str) -> tuple[Capability, ...]:
        return tuple(c for c in self._ordered if c.tool == tool)

    def mutating_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(c.key for c in self._ordered if c.mutating)

    def describe(self, args_models: Mapping[str, type[BaseModel]]) -> list[dict[str, Any]]:
        """Public description of the capabilities of the given (registered) tools."""
        return [
            {
                "tool": c.tool,
                "operation": c.operation,
                "description": c.description,
                "risk": c.risk.value,
                "requires_approval": c.requires_approval,
                "approval_reason": c.approval_reason,
                "retry_safe": c.retry_safe,
                "idempotency": c.idempotency.value,
                "mutates": c.mutates.value,
                "verification": c.verification.value,
                "preconditions": list(c.preconditions),
                "input_schema": c.input_schema(args_models[c.tool]),
                "output_schema": c.output_schema(),
            }
            for c in self._ordered
            if c.tool in args_models
        ]


_WORKSPACE_PATH = (
    "path is relative to the workspace, contains no '..', and resolves inside it",
    "database files and protected paths are refused",
)

CAPABILITIES = CapabilityRegistry(
    (
        Capability(
            tool="company_api",
            operation="get_employee",
            description="Read one employee record.",
            risk=RiskLevel.READ,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.NOT_REQUIRED,
            mutates=StateScope.NONE,
            verification=VerificationStrategy.NONE,
            required_inputs=("employee_id",),
            preconditions=("employee exists",),
            output_model=EmployeeOutput,
        ),
        Capability(
            tool="company_api",
            operation="update_employee",
            description="Change fields of one employee record.",
            risk=RiskLevel.WRITE,
            requires_approval=True,
            approval_reason="Employee record mutation requires human approval.",
            # Retry-safe because injected failures happen before any mutation and
            # the write is keyed, so a retry replays instead of writing twice.
            retry_safe=True,
            idempotency=IdempotencyStrategy.KEYED_TRANSACTION,
            mutates=StateScope.COMPANY,
            verification=VerificationStrategy.EMPLOYEE_FIELDS,
            required_inputs=("employee_id", "fields"),
            preconditions=(
                "employee exists",
                "fields only: " + ", ".join(sorted(EMPLOYEE_UPDATE_FIELDS)),
                "every value is a non-empty string",
                "human approval bound to the exact action",
            ),
            output_model=EmployeeWriteOutput,
            input_constraints=MappingProxyType(
                {
                    "fields": MappingProxyType(
                        {
                            "minProperties": 1,
                            "propertyNames": {"enum": sorted(EMPLOYEE_UPDATE_FIELDS)},
                            "additionalProperties": {"type": "string", "minLength": 1},
                        }
                    )
                }
            ),
        ),
        Capability(
            tool="company_api",
            operation="find_customer",
            description="Search customers by ID, company name or email.",
            risk=RiskLevel.READ,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.NOT_REQUIRED,
            mutates=StateScope.NONE,
            verification=VerificationStrategy.NONE,
            any_of_inputs=("customer_id", "company", "email"),
            preconditions=("at least one of customer_id, company or email",),
            output_model=CustomerSearchOutput,
        ),
        Capability(
            tool="company_api",
            operation="create_ticket",
            description="Create a support ticket for an existing customer.",
            risk=RiskLevel.WRITE,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.KEYED_TRANSACTION,
            mutates=StateScope.COMPANY,
            verification=VerificationStrategy.TICKET_RECORD,
            required_inputs=("customer_id", "subject", "description"),
            optional_inputs=("priority",),
            preconditions=("customer exists",),
            output_model=TicketWriteOutput,
        ),
        Capability(
            tool="company_api",
            operation="get_ticket",
            description="Read one support ticket.",
            risk=RiskLevel.READ,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.NOT_REQUIRED,
            mutates=StateScope.NONE,
            verification=VerificationStrategy.NONE,
            required_inputs=("ticket_id",),
            preconditions=("ticket exists",),
            output_model=TicketOutput,
        ),
        Capability(
            tool="file",
            operation="read",
            description="Read a UTF-8 text file from the workspace.",
            risk=RiskLevel.READ,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.NOT_REQUIRED,
            mutates=StateScope.NONE,
            verification=VerificationStrategy.NONE,
            required_inputs=("path",),
            preconditions=(*_WORKSPACE_PATH, "file exists"),
            output_model=FileReadOutput,
        ),
        Capability(
            tool="file",
            operation="write",
            description="Create or overwrite a text file in the workspace.",
            risk=RiskLevel.WRITE,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.DETERMINISTIC_OVERWRITE,
            mutates=StateScope.WORKSPACE,
            verification=VerificationStrategy.FILE_CONTENT,
            required_inputs=("path", "content"),
            preconditions=_WORKSPACE_PATH,
            output_model=FileWriteOutput,
        ),
        Capability(
            tool="file",
            operation="list",
            description="List files in the workspace (database files are excluded).",
            risk=RiskLevel.READ,
            requires_approval=False,
            retry_safe=True,
            idempotency=IdempotencyStrategy.NOT_REQUIRED,
            mutates=StateScope.NONE,
            verification=VerificationStrategy.NONE,
            optional_inputs=("path",),
            preconditions=_WORKSPACE_PATH,
            output_model=FileListOutput,
        ),
    )
)
