"""API v1 routes: a thin HTTP layer over the existing runtime.

There is deliberately no endpoint that runs an arbitrary tool, shell command or
plan, writes files, or accepts a filesystem path other than a workspace file
name to read. Tasks can only come from goals accepted by the mock planner.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.api.errors import ApiError
from app.api.schemas import (
    TASK_ID_PATTERN,
    ApproveRequest,
    CustomersResponse,
    EmployeesResponse,
    ErrorResponse,
    HealthResponse,
    HistoryResponse,
    RejectRequest,
    ResumeRequest,
    ScenarioOut,
    ScenariosResponse,
    SessionCreateRequest,
    SessionResponse,
    TaskCreateRequest,
    TaskDetail,
    TaskListResponse,
    TicketsResponse,
    VerificationResponse,
    WorkspaceFileContent,
    WorkspaceFilesResponse,
)
from app.api.service import IDEMPOTENCY_KEY, TaskService, Tenant
from app.api.sessions import SESSION_COOKIE, SessionManager
from app.config import Settings
from app.llm.mock import SUPPORTED_GOALS
from app.planning import InterpretationError, PlanningError

LOCAL_APPROVER = "local-dashboard"

SCENARIOS = (
    ScenarioOut(
        scenario_id="ticket",
        title="Support ticket for Acme Corp",
        description=(
            "Finds customer Acme Corp, creates a $240 billing-discrepancy ticket and "
            "optionally writes a summary note. Supports one injected (simulated) 503."
        ),
        goals=[
            "Create a support ticket for Acme Corp about a $240 billing discrepancy.",
            "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
            "and save a summary note.",
        ],
        requires_approval=False,
        injectable_operation="create_ticket",
    ),
    ScenarioOut(
        scenario_id="employee",
        title="Employee E-17 title update",
        description=(
            "Changes employee E-17's title to Senior Engineer. The policy gate pauses "
            "before the write until a human approves or rejects it."
        ),
        goals=[
            "Update employee E-17 title to Senior Engineer.",
            "Update employee E-17 title to Senior Engineer and confirm the change in a "
            "note file.",
        ],
        requires_approval=True,
        injectable_operation=None,
    ),
)

PUBLIC_NOTICE = (
    "Public demo: every browser session gets a private sandbox of fictional company "
    "data. Nothing is sent to real systems. State is temporary: it is erased after "
    "{ttl} minutes of inactivity and whenever the server restarts or sleeps."
)
LOCAL_NOTICE = (
    "Local mode: tasks, audit events and mock company data persist in the local "
    "SQLite database shared with the CLI."
)

_ERRORS = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]


@dataclass
class AppContext:
    settings: Settings
    service: TaskService
    sessions: SessionManager | None
    boot_id: str
    started_at: datetime

    @property
    def public(self) -> bool:
        return self.settings.app_mode == "public"


def context(request: Request) -> AppContext:
    return request.app.state.aae


def tenant(request: Request, ctx: Annotated[AppContext, Depends(context)]) -> Tenant:
    if not ctx.public:
        return Tenant("local", ctx.settings, LOCAL_APPROVER)
    assert ctx.sessions is not None
    session = ctx.sessions.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise ApiError(
            401,
            "session_required",
            "No active demo session. Sessions expire after inactivity and all demo "
            "state is erased when the server restarts; start a new session.",
        )
    return Tenant(
        session.session_id,
        SessionManager.settings_for(session, ctx.settings),
        session.approver,
        task_quota=ctx.settings.public_max_tasks_per_session,
    )


Ctx = Annotated[AppContext, Depends(context)]
CurrentTenant = Annotated[Tenant, Depends(tenant)]

router = APIRouter(prefix="/api/v1", responses=_ERRORS)


# --- Meta -------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health(ctx: Ctx) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        mode=ctx.settings.app_mode,
        persistence="ephemeral" if ctx.public else "persistent",
        boot_id=ctx.boot_id,
        started_at=ctx.started_at,
    )


@router.get("/scenarios", response_model=ScenariosResponse, tags=["meta"])
def scenarios(ctx: Ctx) -> ScenariosResponse:
    return ScenariosResponse(
        scenarios=list(SCENARIOS),
        supported_formats=list(SUPPORTED_GOALS),
        failure_injection_enabled=ctx.settings.allow_failure_injection,
    )


def _session_response(ctx: AppContext, current: Tenant | None) -> SessionResponse:
    if not ctx.public:
        return SessionResponse(
            mode="local",
            authenticated=False,
            approver=LOCAL_APPROVER,
            access_code_required=False,
            expires_after_idle_seconds=None,
            task_quota=None,
            tasks_used=None,
            notice=LOCAL_NOTICE,
        )
    ttl = ctx.settings.public_session_ttl_seconds
    tasks_used = None
    if current is not None:
        tasks_used = ctx.service.open(current).store.count_tasks()
    return SessionResponse(
        mode="public",
        authenticated=current is not None,
        approver=current.approver if current else None,
        access_code_required=bool(ctx.settings.public_access_code),
        expires_after_idle_seconds=ttl,
        task_quota=ctx.settings.public_max_tasks_per_session,
        tasks_used=tasks_used,
        notice=PUBLIC_NOTICE.format(ttl=ttl // 60),
    )


@router.get("/session", response_model=SessionResponse, tags=["session"])
def get_session(request: Request, ctx: Ctx) -> SessionResponse:
    current = None
    if ctx.public:
        try:
            current = tenant(request, ctx)
        except ApiError:
            current = None
    return _session_response(ctx, current)


@router.post("/session", response_model=SessionResponse, status_code=201, tags=["session"])
def create_session(body: SessionCreateRequest, response: Response, ctx: Ctx) -> SessionResponse:
    if not ctx.public:
        raise ApiError(404, "not_found", "Sessions exist only in public demo mode.")
    assert ctx.sessions is not None
    expected = ctx.settings.public_access_code
    if expected:
        if not hmac.compare_digest((body.access_code or "").encode(), expected.encode()):
            raise ApiError(403, "invalid_access_code", "The access code is not valid.")
    token, session = ctx.sessions.create(ctx.service.jobs.tenant_busy)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ctx.settings.public_session_ttl_seconds,
        httponly=True,
        secure=ctx.settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    current = Tenant(
        session.session_id,
        SessionManager.settings_for(session, ctx.settings),
        session.approver,
        task_quota=ctx.settings.public_max_tasks_per_session,
    )
    return _session_response(ctx, current)


# --- Tasks ------------------------------------------------------------------------


@router.post(
    "/tasks",
    response_model=TaskDetail,
    status_code=202,
    tags=["tasks"],
    responses={200: {"model": TaskDetail, "description": "Idempotent replay"}},
)
async def create_task(
    body: TaskCreateRequest,
    response: Response,
    ctx: Ctx,
    current: CurrentTenant,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskDetail:
    if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ApiError(
            422, "invalid_idempotency_key", "Idempotency-Key must be 8-128 of [A-Za-z0-9_-]."
        )
    rt = await run_in_threadpool(ctx.service.open, current)
    try:
        # The async variant: the sync interpret_and_plan calls asyncio.run, which
        # cannot run inside this event loop. The mock planner performs no I/O.
        planned = await rt.planning().interpret_and_plan_async(body.goal)
    except (InterpretationError, PlanningError) as exc:
        raise ApiError(
            422, "goal_rejected", f"Goal rejected; no task was created. {exc}"
        ) from None
    detail = await run_in_threadpool(
        ctx.service.submit,
        current,
        goal=body.goal,
        planned=planned,
        inject_failure=body.inject_failure,
        idempotency_key=idempotency_key,
    )
    if detail.idempotent_replay:
        response.status_code = 200
    response.headers["Location"] = f"/api/v1/tasks/{detail.task_id}"
    return detail


@router.get("/tasks", response_model=TaskListResponse, tags=["tasks"])
def list_tasks(
    ctx: Ctx,
    current: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> TaskListResponse:
    return ctx.service.list_tasks(current, limit=limit, offset=offset)


@router.get("/tasks/{task_id}", response_model=TaskDetail, tags=["tasks"])
def get_task(task_id: TaskId, ctx: Ctx, current: CurrentTenant) -> TaskDetail:
    return ctx.service.get(current, task_id)


@router.get("/tasks/{task_id}/history", response_model=HistoryResponse, tags=["tasks"])
def task_history(task_id: TaskId, ctx: Ctx, current: CurrentTenant) -> HistoryResponse:
    return ctx.service.history(current, task_id)


@router.get(
    "/tasks/{task_id}/verification", response_model=VerificationResponse, tags=["tasks"]
)
def task_verification(task_id: TaskId, ctx: Ctx, current: CurrentTenant) -> VerificationResponse:
    return ctx.service.verification(current, task_id)


@router.post("/tasks/{task_id}/approve", response_model=TaskDetail, tags=["approval"])
def approve_task(
    task_id: TaskId, body: ApproveRequest, ctx: Ctx, current: CurrentTenant
) -> TaskDetail:
    return ctx.service.approve(current, task_id, body.step_id, body.fingerprint)


@router.post("/tasks/{task_id}/reject", response_model=TaskDetail, tags=["approval"])
def reject_task(
    task_id: TaskId, body: RejectRequest, ctx: Ctx, current: CurrentTenant
) -> TaskDetail:
    return ctx.service.reject(current, task_id, body.step_id, body.reason, body.fingerprint)


@router.post("/tasks/{task_id}/resume", response_model=TaskDetail, tags=["tasks"])
def resume_task(
    task_id: TaskId, body: ResumeRequest, ctx: Ctx, current: CurrentTenant
) -> TaskDetail:
    return ctx.service.resume(current, task_id)


# --- Mock company and workspace (read-only) ---------------------------------------


@router.get("/company/employees", response_model=EmployeesResponse, tags=["company"])
def employees(ctx: Ctx, current: CurrentTenant) -> EmployeesResponse:
    return EmployeesResponse(employees=ctx.service.employees(current))


@router.get("/company/customers", response_model=CustomersResponse, tags=["company"])
def customers(ctx: Ctx, current: CurrentTenant) -> CustomersResponse:
    return CustomersResponse(customers=ctx.service.customers(current))


@router.get("/company/tickets", response_model=TicketsResponse, tags=["company"])
def tickets(ctx: Ctx, current: CurrentTenant) -> TicketsResponse:
    return TicketsResponse(tickets=ctx.service.tickets(current))


@router.get("/workspace/files", response_model=WorkspaceFilesResponse, tags=["workspace"])
def workspace_files(ctx: Ctx, current: CurrentTenant) -> WorkspaceFilesResponse:
    return WorkspaceFilesResponse(files=ctx.service.workspace_files(current))


@router.get("/workspace/file", response_model=WorkspaceFileContent, tags=["workspace"])
def workspace_file(
    ctx: Ctx,
    current: CurrentTenant,
    path: Annotated[str, Query(min_length=1, max_length=256)],
) -> WorkspaceFileContent:
    return ctx.service.workspace_file(current, path)
