"""HTTP request and response models (API v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

STEP_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
TASK_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
FINGERPRINT_PATTERN = r"^sha256:[0-9a-f]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Requests -----------------------------------------------------------------


class TaskCreateRequest(_Strict):
    goal: str = Field(min_length=1, max_length=300)
    # Demo control: one simulated transient 503 on this company operation.
    inject_failure: Literal["create_ticket"] | None = None


class ApproveRequest(_Strict):
    step_id: str = Field(pattern=STEP_ID_PATTERN)
    confirm: Literal[True]
    # Optional: the pending action's fingerprint as shown to the approver.
    fingerprint: str | None = Field(default=None, pattern=FINGERPRINT_PATTERN)


class RejectRequest(_Strict):
    step_id: str = Field(pattern=STEP_ID_PATTERN)
    reason: str = Field(default="Rejected via dashboard", min_length=1, max_length=300)
    confirm: Literal[True]
    fingerprint: str | None = Field(default=None, pattern=FINGERPRINT_PATTERN)


class ResumeRequest(_Strict):
    confirm: Literal[True]


class SessionCreateRequest(_Strict):
    access_code: str | None = Field(default=None, max_length=128)


# --- Responses ----------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    mode: Literal["local", "public"]
    persistence: Literal["persistent", "ephemeral"]
    boot_id: str
    started_at: datetime


class ScenarioOut(BaseModel):
    scenario_id: str
    title: str
    description: str
    goals: list[str]
    requires_approval: bool
    injectable_operation: str | None


class ScenariosResponse(BaseModel):
    scenarios: list[ScenarioOut]
    supported_formats: list[str]
    failure_injection_enabled: bool


class CapabilityOut(BaseModel):
    tool: str
    operation: str
    description: str
    risk: Literal["read", "write", "destructive"]
    # Always required before the operation runs.
    requires_approval: bool
    approval_reason: str | None
    # Approval required only for some arguments (e.g. closing a ticket), described here.
    approval_condition: str | None
    retry_safe: bool
    idempotency: Literal["not_required", "keyed_transaction", "deterministic_overwrite"]
    mutates: Literal["none", "company", "workspace"]
    verification: Literal[
        "none",
        "employee_fields",
        "ticket_record",
        "file_content",
        "ticket_fields",
        "ticket_assignment",
        "customer_record",
        "employee_status",
    ]
    preconditions: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


class CapabilitiesResponse(BaseModel):
    schema_version: Literal["1"]
    capabilities: list[CapabilityOut]


class SessionResponse(BaseModel):
    mode: Literal["local", "public"]
    authenticated: bool
    approver: str | None
    access_code_required: bool
    expires_after_idle_seconds: int | None
    task_quota: int | None
    tasks_used: int | None
    notice: str


class CriterionOut(BaseModel):
    description: str
    criterion_type: str
    target: str | None
    expected_value: Any


class InterpretedGoalOut(BaseModel):
    objective: str
    entities: dict[str, Any]
    constraints: list[str]
    required_information: list[str]
    success_criteria: list[CriterionOut]


class PlanStepOut(BaseModel):
    step_id: str
    description: str
    tool: str | None
    operation: str | None
    arguments: dict[str, Any]
    expected_outcome: str
    status: str
    attempts: int


class ToolCallOut(BaseModel):
    call_id: str
    step_id: str | None
    tool: str
    operation: str | None
    arguments: dict[str, Any]
    status: str
    attempt: int | None
    started_at: datetime
    completed_at: datetime | None
    error_type: str | None
    error_message: str | None


class PendingActionOut(BaseModel):
    step_id: str | None
    tool: str
    operation: str | None
    arguments: dict[str, Any]
    risk: str
    reason: str | None
    fingerprint: str
    created_at: datetime


class FailureOut(BaseModel):
    failure_id: str
    step_id: str | None
    failure_type: str
    message: str
    retryable: bool
    recovery_action: str | None
    timestamp: datetime


class TaskSummary(BaseModel):
    task_id: str
    status: str
    user_goal: str | None
    created_at: str
    updated_at: str
    approval_status: str | None
    verification_status: str | None
    retry_count: int | None
    steps_completed: int | None
    steps_total: int | None
    readable: bool


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
    total: int
    limit: int
    offset: int


class TaskDetail(BaseModel):
    task_id: str
    status: str
    user_goal: str
    created_at: datetime
    updated_at: datetime
    interpreted_goal: InterpretedGoalOut | None
    plan: list[PlanStepOut]
    current_step: int
    tool_calls: list[ToolCallOut]
    retry_count: int
    failures: list[FailureOut]
    approval_status: str
    pending_action: PendingActionOut | None
    verification_status: str
    verification_summary: str | None
    verification_checks_passed: int
    verification_checks_total: int
    evidence_count: int
    completion_summary: str | None
    terminal: bool
    executing: bool
    resumable: bool
    idempotent_replay: bool = False


class EventOut(BaseModel):
    event_id: str
    timestamp: datetime
    event_type: str
    action: str | None
    tool: str | None
    actor: str
    arguments: dict[str, Any] | None
    result: dict[str, Any] | None
    observation: dict[str, Any] | None
    failure: dict[str, Any] | None
    verification_result: dict[str, Any] | None


class HistoryResponse(BaseModel):
    task_id: str
    count: int
    events: list[EventOut]


class VerificationCheckOut(BaseModel):
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""


class EvidenceOut(BaseModel):
    evidence_id: str
    type: str
    description: str
    data: dict[str, Any]
    timestamp: datetime


class VerificationResponse(BaseModel):
    task_id: str
    task_status: str
    status: str
    summary: str | None
    checks_passed: int
    checks_total: int
    checks: list[VerificationCheckOut]
    failure_reasons: list[str]
    evidence: list[EvidenceOut]


class EmployeeOut(BaseModel):
    employee_id: str
    name: str
    email: str
    title: str
    department: str
    status: str
    updated_at: datetime


class CustomerOut(BaseModel):
    customer_id: str
    name: str
    email: str
    company: str
    created_at: datetime


class TicketOut(BaseModel):
    ticket_id: str
    customer_id: str
    subject: str
    description: str
    status: str
    priority: str
    assignee_id: str | None
    created_at: datetime
    updated_at: datetime


class EmployeesResponse(BaseModel):
    employees: list[EmployeeOut]


class CustomersResponse(BaseModel):
    customers: list[CustomerOut]


class TicketsResponse(BaseModel):
    tickets: list[TicketOut]


class WorkspaceFileOut(BaseModel):
    path: str
    size_bytes: int


class WorkspaceFilesResponse(BaseModel):
    files: list[WorkspaceFileOut]


class WorkspaceFileContent(BaseModel):
    path: str
    size_bytes: int
    content: str
