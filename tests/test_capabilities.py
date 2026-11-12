"""Capability registry (v0.3.0 milestone 1): coverage, contracts, equivalence with
the pre-migration decisions, fail-closed unknown operations, and discovery."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.cli import RUNTIME_TOOLS, build_runtime, main
from app.config import Settings
from app.main import create_app
from app.models.enums import FailureType, TaskStatus, ToolCallStatus
from app.models.plan import Plan, PlanStep
from app.models.records import ToolCallRecord
from app.models.state import ExecutionState
from app.planning.validator import PlanValidationError, compact_tool_catalog, validate_plan
from app.policy import ApprovalPolicy
from app.runtime import ExecutionRuntime
from app.tools import CompanyAPITool, FileTool, RiskLevel, ToolRegistry
from app.tools.capabilities import (
    CAPABILITIES,
    Capability,
    CapabilityRegistry,
    IdempotencyStrategy,
    StateScope,
    UnknownCapabilityError,
    VerificationStrategy,
)
from app.tools.company import CompanyAPIArgs, CompanyOperation
from app.tools.file import FileOperation, FileToolArgs
from app.verify.rules import MUTATING_OPERATIONS, derive_criteria_from_execution
from app.world import EMPLOYEE_UPDATE_FIELDS

# The eight operations that existed before the registry (v0.3.0 milestone 1).
ORIGINAL_OPERATIONS = [
    ("company_api", "get_employee"),
    ("company_api", "update_employee"),
    ("company_api", "find_customer"),
    ("company_api", "create_ticket"),
    ("company_api", "get_ticket"),
    ("file", "read"),
    ("file", "write"),
    ("file", "list"),
]
# Added in milestone 2.
NEW_OPERATIONS = [
    ("company_api", "list_tickets"),
    ("company_api", "update_ticket"),
    ("company_api", "assign_ticket"),
    ("company_api", "create_customer"),
    ("company_api", "deactivate_employee"),
]
ALL_OPERATIONS = ORIGINAL_OPERATIONS[:5] + NEW_OPERATIONS + ORIGINAL_OPERATIONS[5:]
ARGS_MODELS = {"company_api": CompanyAPIArgs, "file": FileToolArgs}
SAMPLE_INPUTS: dict[str, Any] = {
    "employee_id": "E-17",
    "fields": {"title": "Senior Engineer"},
    "customer_id": "C-1001",
    "company": "Acme Corp",
    "email": "billing@acme.example",
    "subject": "Billing discrepancy $240",
    "description": "Acme reported a $240 billing discrepancy.",
    "priority": "high",
    "ticket_id": "T-10001",
    "path": "note.txt",
    "content": "hello",
    "filters": {"status": "open"},
    "limit": 10,
    "offset": 0,
    "name": "Pat Lee",
}
SAMPLE_OVERRIDES: dict[str, dict[str, Any]] = {
    "update_ticket": {"fields": {"status": "in_progress"}},
    "assign_ticket": {"employee_id": "E-31"},
    "create_customer": {"company": "Nimbus Labs", "email": "pat.lee@nimbus.example"},
    "deactivate_employee": {"employee_id": "E-23"},
}

# Decisions recorded on commit 03124a6, before the registry existed:
# (policy with tool: approval, risk, reason), (policy without tool: approval, risk),
# tool.risk_for(op), tool.is_retry_safe({"operation": op}).
NO_APPROVAL = "Operation is not classified as requiring human approval."
PRE_MIGRATION: dict[tuple[str, str], tuple[tuple, tuple, str, bool]] = {
    ("company_api", "get_employee"): ((False, "read", NO_APPROVAL), (False, "write"), "read", True),
    ("company_api", "update_employee"): (
        (True, "write", "Employee record mutation requires human approval."),
        (True, "write"),
        "write",
        True,
    ),
    ("company_api", "find_customer"): ((False, "read", NO_APPROVAL), (False, "write"), "read", True),
    ("company_api", "create_ticket"): ((False, "write", NO_APPROVAL), (False, "write"), "write", True),
    ("company_api", "get_ticket"): ((False, "read", NO_APPROVAL), (False, "write"), "read", True),
    ("file", "read"): ((False, "read", NO_APPROVAL), (False, "write"), "read", True),
    ("file", "write"): ((False, "write", NO_APPROVAL), (False, "write"), "write", True),
    ("file", "list"): ((False, "read", NO_APPROVAL), (False, "write"), "read", True),
}
PRE_MIGRATION_MUTATING = {
    ("company_api", "create_ticket"),
    ("company_api", "update_employee"),
    ("file", "write"),
}
PRE_MIGRATION_METADATA = {
    "company_api": {
        "operations": [
            ("get_employee", "read"),
            ("update_employee", "write"),
            ("find_customer", "read"),
            ("create_ticket", "write"),
            ("get_ticket", "read"),
        ],
        "retry_safe_operations": [
            "create_ticket",
            "find_customer",
            "get_employee",
            "get_ticket",
            "update_employee",
        ],
        "risk_level": "write",
        "retry_safe": True,
    },
    "file": {
        "operations": [("read", "read"), ("write", "write"), ("list", "read")],
        "risk_level": "write",
        "retry_safe": True,
    },
}


@pytest.fixture
def tools(registry: ToolRegistry) -> dict[str, Any]:
    return {"company_api": registry.get("company_api"), "file": registry.get("file")}


# --- Coverage and structure ------------------------------------------------------------


def test_registry_covers_exactly_the_registered_operations() -> None:
    assert [c.key for c in CAPABILITIES] == ALL_OPERATIONS
    assert len(CAPABILITIES) == 13
    assert {op for t, op in ALL_OPERATIONS if t == "company_api"} == set(get_args(CompanyOperation))
    assert {op for t, op in ALL_OPERATIONS if t == "file"} == set(get_args(FileOperation))


def test_original_operations_keep_their_positions() -> None:
    for tool in ("company_api", "file"):
        original = [op for t, op in ORIGINAL_OPERATIONS if t == tool]
        assert [c.operation for c in CAPABILITIES.for_tool(tool)][: len(original)] == original


def test_every_capability_is_fully_described() -> None:
    for c in CAPABILITIES:
        assert c.description and c.output_model is not None
        assert isinstance(c.risk, RiskLevel)
        assert isinstance(c.idempotency, IdempotencyStrategy)
        assert isinstance(c.verification, VerificationStrategy)
        assert isinstance(c.mutates, StateScope)
        assert (c.approval_reason is not None) == c.requires_approval
        assert (c.approval_rule is None) == (c.approval_condition is None)
        assert not (c.requires_approval and c.approval_rule is not None)
        assert c.strict_inputs == (c.key in NEW_OPERATIONS)
        assert c.mutating == (c.verification is not VerificationStrategy.NONE)
        assert c.mutating == (c.idempotency is not IdempotencyStrategy.NOT_REQUIRED)
        assert c.mutating == (c.risk is not RiskLevel.READ)


def test_capabilities_are_immutable() -> None:
    capability = CAPABILITIES.require("company_api", "update_employee")
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability.requires_approval = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        capability.input_constraints["fields"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        CAPABILITIES._by_key[("x", "y")] = capability  # type: ignore[index]
    assert isinstance(capability.preconditions, tuple)


def test_duplicate_capabilities_are_rejected() -> None:
    capability = CAPABILITIES.require("file", "read")
    with pytest.raises(ValueError, match="Duplicate"):
        CapabilityRegistry((capability, capability))


# --- Input and output contracts --------------------------------------------------------


def minimal_input(c: Capability) -> dict[str, Any]:
    samples = {**SAMPLE_INPUTS, **SAMPLE_OVERRIDES.get(c.operation, {})}
    payload = {"operation": c.operation, **{f: samples[f] for f in c.required_inputs}}
    if c.any_of_inputs:
        payload[c.any_of_inputs[0]] = samples[c.any_of_inputs[0]]
    return payload


@pytest.mark.parametrize("key", NEW_OPERATIONS, ids=lambda k: k[1])
def test_new_operations_reject_undeclared_arguments(key: tuple[str, str]) -> None:
    capability = CAPABILITIES.require(*key)
    payload = {**minimal_input(capability), "subject": "not an input of this operation"}
    if "subject" in capability.input_fields:
        payload = {**minimal_input(capability), "sql": "1=1"}
    with pytest.raises(ValidationError, match="Unsupported argument"):
        CompanyAPIArgs.model_validate(payload)
    assert capability.input_schema(CompanyAPIArgs)["additionalProperties"] is False


def test_original_operations_still_ignore_undeclared_arguments() -> None:
    CompanyAPIArgs.model_validate({"operation": "get_employee", "employee_id": "E-17", "limit": 9999})


@pytest.mark.parametrize("capability", list(CAPABILITIES), ids=lambda c: f"{c.tool}.{c.operation}")
def test_input_schema_matches_the_tool_contract(capability: Capability) -> None:
    model = ARGS_MODELS[capability.tool]
    model.model_validate(minimal_input(capability))
    for name in capability.required_inputs:
        payload = minimal_input(capability)
        del payload[name]
        with pytest.raises(ValidationError):
            model.model_validate(payload)
    if capability.any_of_inputs:
        payload = minimal_input(capability)
        for name in capability.any_of_inputs:
            payload.pop(name, None)
        with pytest.raises(ValidationError):
            model.model_validate(payload)

    schema = capability.input_schema(model)
    source = model.model_json_schema()["properties"]
    assert schema["properties"]["operation"] == {"const": capability.operation, "type": "string"}
    assert schema["required"] == ["operation", *capability.required_inputs]
    for name in capability.input_fields:
        assert source[name].items() <= schema["properties"][name].items() or name in (
            capability.input_constraints
        )
    assert set(schema["properties"]) - {"operation"} <= set(source)


def test_employee_update_schema_lists_exactly_the_supported_fields() -> None:
    fields = CAPABILITIES.require("company_api", "update_employee").input_schema(CompanyAPIArgs)[
        "properties"
    ]["fields"]
    assert fields["propertyNames"]["enum"] == sorted(EMPLOYEE_UPDATE_FIELDS)
    assert fields["minProperties"] == 1


def test_real_tool_outputs_match_the_output_schemas(tools: dict[str, Any]) -> None:
    sequence = [
        ("company_api", {"operation": "get_employee", "employee_id": "E-17"}),
        ("company_api", {"operation": "find_customer", "company": "Acme Corp"}),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "update_employee"))),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "create_ticket"))),
        ("company_api", {"operation": "get_ticket", "ticket_id": "T-10001"}),
        ("file", {"operation": "write", "path": "note.txt", "content": "hello"}),
        ("file", {"operation": "read", "path": "note.txt"}),
        ("file", {"operation": "list"}),
        ("company_api", {"operation": "list_tickets", "filters": {"status": "open"}, "limit": 5}),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "assign_ticket"))),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "update_ticket"))),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "create_customer"))),
        ("company_api", minimal_input(CAPABILITIES.require("company_api", "deactivate_employee"))),
    ]
    seen = set()
    for tool_name, args in sequence:
        result = tools[tool_name].invoke(args, idempotency_key=f"t:{args['operation']}")
        assert result.ok, result.error_message
        capability = CAPABILITIES.require(tool_name, args["operation"])
        capability.output_model.model_validate(result.data)
        seen.add(capability.key)
    assert seen == set(ALL_OPERATIONS)


# --- Equivalence with the pre-migration behaviour ---------------------------------------


@pytest.mark.parametrize("key", ORIGINAL_OPERATIONS, ids=lambda k: ".".join(k))
def test_decisions_match_pre_migration_behaviour(key: tuple[str, str], tools: dict[str, Any]) -> None:
    tool_name, operation = key
    with_tool, without_tool, risk, retry_safe = PRE_MIGRATION[key]
    policy = ApprovalPolicy()
    args = {"operation": operation}

    decision = policy.evaluate(tool_name=tool_name, arguments=args, tool=tools[tool_name])
    assert (decision.requires_approval, decision.risk.value, decision.reason) == with_tool
    bare = policy.evaluate(tool_name=tool_name, arguments=args, tool=None)
    assert (bare.requires_approval, bare.risk.value) == without_tool
    assert tools[tool_name].risk_for(operation).value == risk
    assert tools[tool_name].is_retry_safe(args) is retry_safe

    capability = CAPABILITIES.require(tool_name, operation)
    assert capability.requires_approval == with_tool[0]
    assert capability.risk.value == risk
    assert capability.retry_safe is retry_safe


NEW_MUTATING = {
    ("company_api", "update_ticket"),
    ("company_api", "assign_ticket"),
    ("company_api", "create_customer"),
    ("company_api", "deactivate_employee"),
}


def test_mutating_operations_extend_pre_migration() -> None:
    assert MUTATING_OPERATIONS == CAPABILITIES.mutating_keys() == PRE_MIGRATION_MUTATING | NEW_MUTATING
    assert {k for k in MUTATING_OPERATIONS if k in ORIGINAL_OPERATIONS} == PRE_MIGRATION_MUTATING


def test_tool_metadata_and_planner_catalog_extend_pre_migration(
    registry: ToolRegistry, tools: dict[str, Any]
) -> None:
    """The original operations keep their entries and order; new ones follow."""
    catalog = compact_tool_catalog(registry)
    for name, expected in PRE_MIGRATION_METADATA.items():
        meta = tools[name].metadata()
        original = expected["operations"]
        assert list(meta["operations"].items())[: len(original)] == original
        assert list(catalog[name]["operations"].items())[: len(original)] == original
        added = [(c.operation, c.risk.value) for c in CAPABILITIES.for_tool(name)][len(original):]
        assert list(meta["operations"].items())[len(original):] == added
        assert meta["risk_level"] == expected["risk_level"]
        assert meta["retry_safe"] is expected["retry_safe"]
        if "retry_safe_operations" in expected:
            originals = {op for op, _ in original}
            assert [
                op for op in meta["retry_safe_operations"] if op in originals
            ] == expected["retry_safe_operations"]
        else:
            assert "retry_safe_operations" not in meta


def test_verification_strategies_match_the_criteria_the_rules_derive() -> None:
    criterion_for = {
        VerificationStrategy.EMPLOYEE_FIELDS: "employee_field",
        VerificationStrategy.TICKET_RECORD: "ticket_exists",
        VerificationStrategy.FILE_CONTENT: "file_exists",
        VerificationStrategy.TICKET_FIELDS: "ticket_fields",
        VerificationStrategy.TICKET_ASSIGNMENT: "ticket_assignment",
        VerificationStrategy.CUSTOMER_RECORD: "customer_record",
        VerificationStrategy.EMPLOYEE_STATUS: "employee_status",
    }
    for capability in CAPABILITIES:
        call = ToolCallRecord(
            task_id="t",
            step_id="s",
            tool_name=capability.tool,
            arguments=minimal_input(capability),
            status=ToolCallStatus.SUCCEEDED,
            result={"data": {"ticket": {"ticket_id": "T-1"}}},
        )
        derived = derive_criteria_from_execution(
            ExecutionState(user_goal="g", tool_calls=[call])
        )
        expected = criterion_for.get(capability.verification)
        assert [c.criterion_type for c in derived] == ([expected] if expected else [])


# --- Unknown operations gain nothing ---------------------------------------------------


UNKNOWN = [("company_api", "delete_employee"), ("file", "delete"), ("company_api", "")]


@pytest.mark.parametrize("key", UNKNOWN, ids=lambda k: ".".join(k) or "empty")
def test_unknown_operations_fail_closed(key: tuple[str, str], tools: dict[str, Any]) -> None:
    tool_name, operation = key
    assert CAPABILITIES.find(tool_name, operation) is None
    assert key not in CAPABILITIES
    with pytest.raises(UnknownCapabilityError):
        CAPABILITIES.require(tool_name, operation)
    with pytest.raises(UnknownCapabilityError):
        tools[tool_name].risk_for(operation)
    assert tools[tool_name].is_retry_safe({"operation": operation}) is False

    decision = ApprovalPolicy().evaluate(
        tool_name=tool_name, arguments={"operation": operation}, tool=tools[tool_name]
    )
    # Before the registry this was classified "read"; now it gets the tool's
    # conservative risk level and never a read classification.
    assert decision.risk is RiskLevel.WRITE
    with pytest.raises(ValidationError):
        ARGS_MODELS[tool_name].model_validate({"operation": operation, "path": "a.txt"})


@pytest.mark.parametrize("args", [None, {}, {"operation": None}, {"operation": 7}])
def test_missing_operation_is_never_retry_safe(args: Any, tools: dict[str, Any]) -> None:
    for tool in tools.values():
        assert tool.is_retry_safe(args) is False


def test_unknown_operation_is_rejected_by_plan_validation(registry: ToolRegistry) -> None:
    plan = Plan(
        steps=[PlanStep(step_id="s", description="x", tool_name="file", arguments={"operation": "delete", "path": "a"})]
    )
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)


def test_unknown_operation_at_runtime_fails_without_side_effects(
    runtime: ExecutionRuntime, workspace: Path
) -> None:
    from app.authz import AuthorizationError, AuthorizationReason

    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    plan = [
        PlanStep(
            step_id="s",
            description="x",
            tool_name="file",
            arguments={"operation": "delete", "path": "keep.txt"},
        )
    ]
    # Goal-scoped authorization rejects unknown operations before any side effects.
    with pytest.raises(AuthorizationError) as caught:
        runtime.create_task(user_goal="delete", plan=plan)
    assert caught.value.reason is AuthorizationReason.UNKNOWN_OPERATION
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_unregistered_tool_policy_is_unchanged() -> None:
    decision = ApprovalPolicy().evaluate(tool_name="ghost", arguments={"operation": "x"}, tool=None)
    assert (decision.requires_approval, decision.risk) == (False, RiskLevel.WRITE)


# --- Discovery endpoint -------------------------------------------------------------------


def test_runtime_tools_are_exactly_the_registered_tools(tmp_path: Path) -> None:
    rt = build_runtime(
        Settings(
            _env_file=None,
            database_path=str(tmp_path / "d" / "runtime.db"),
            workspace_path=str(tmp_path / "w"),
        )
    )
    assert sorted(t.name for t in RUNTIME_TOOLS) == sorted(t.name for t in rt.registry.list_tools())
    assert set(RUNTIME_TOOLS) == {CompanyAPITool, FileTool}


def test_describe_lists_only_registered_tools() -> None:
    described = CAPABILITIES.describe({"file": FileToolArgs})
    assert [(d["tool"], d["operation"]) for d in described] == [
        k for k in ALL_OPERATIONS if k[0] == "file"
    ]


def local_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "local" / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "local" / "ws-sentinel-dir"),
        allowed_hosts="testserver",
        xai_api_key="xai-TEST-SENTINEL-KEY",
    )


def public_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_mode="public",
        allowed_hosts="testserver",
        public_data_root=str(tmp_path / "public"),
        public_access_code="ACCESS-SENTINEL-CODE",
        database_path=str(tmp_path / "unused" / "runtime.db"),
        workspace_path=str(tmp_path / "ws-sentinel-unused"),
        xai_api_key="xai-TEST-SENTINEL-KEY",
    )


def test_discovery_endpoint_contract_and_determinism(tmp_path: Path) -> None:
    with TestClient(create_app(local_settings(tmp_path))) as first:
        response = first.get("/api/v1/capabilities")
        again = first.get("/api/v1/capabilities")
    with TestClient(create_app(local_settings(tmp_path / "other"))) as second:
        other = second.get("/api/v1/capabilities")
    assert response.status_code == 200
    assert response.content == again.content == other.content
    body = response.json()
    assert body["schema_version"] == "1"
    assert [(c["tool"], c["operation"]) for c in body["capabilities"]] == ALL_OPERATIONS
    update = next(c for c in body["capabilities"] if c["operation"] == "update_employee")
    assert update["requires_approval"] is True and update["risk"] == "write"
    assert update["approval_reason"] == "Employee record mutation requires human approval."
    for item in body["capabilities"]:
        capability = CAPABILITIES.require(item["tool"], item["operation"])
        assert item["risk"] == capability.risk.value
        assert item["retry_safe"] is capability.retry_safe
        assert item["input_schema"] == capability.input_schema(ARGS_MODELS[item["tool"]])
        assert item["output_schema"] == capability.output_schema()
    assert response.headers["cache-control"] == "no-store"
    assert not (tmp_path / "local" / "data" / "runtime.db").exists()


@pytest.mark.parametrize("mode", ["local", "public"])
def test_discovery_exposes_no_implementation_details(tmp_path: Path, mode: str) -> None:
    settings = local_settings(tmp_path) if mode == "local" else public_settings(tmp_path)
    base_url = "http://testserver" if mode == "local" else "https://testserver"
    with TestClient(create_app(settings), base_url=base_url) as client:
        response = client.get("/api/v1/capabilities")
        assert response.status_code == 200
        text = response.text
        assert client.get("/api/v1/capabilities", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/api/v1/capabilities", json={}).status_code == 405
    forbidden = [
        str(tmp_path),
        Path(settings.workspace_path).name,
        "runtime.db",
        "xai-TEST-SENTINEL-KEY",
        "ACCESS-SENTINEL-CODE",
        "workspace_root",
        "app.tools",
        "/tmp",
        "\\",
    ]
    for value in forbidden:
        assert value not in text, value
    assert json.loads(text)["capabilities"]


# --- Existing behaviour is unchanged ------------------------------------------------------


TICKET_GOAL = (
    "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
    "and save a summary note."
)
# Event sequence recorded for this goal with one injected 503 before Phase 4
# (v0.2.0), excluding the dashboard-only demo.failure_injected event.
PRE_MIGRATION_TICKET_EVENTS = (
    ["TASK_CREATED", "authorization.bound", "PLAN_STARTED", "STEP_STARTED", "TOOL_CALLED",
     "TOOL_SUCCEEDED", "STEP_SUCCEEDED", "STEP_STARTED", "TOOL_CALLED", "TOOL_FAILED",
     "failure.detected", "recovery.evaluated", "recovery.retry_started", "TOOL_CALLED",
     "recovery.retry_succeeded", "TOOL_SUCCEEDED", "STEP_SUCCEEDED", "STEP_STARTED",
     "TOOL_CALLED", "TOOL_SUCCEEDED", "STEP_SUCCEEDED", "verification.started"]
    + ["verification.check"] * 6
    + ["evidence.recorded"] * 9
    + ["verification.passed", "TASK_COMPLETED"]
)


def test_deterministic_scenarios_are_unchanged(tmp_path: Path, capsys) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
    )
    assert main(["--json", "run", TICKET_GOAL, "--inject-transient", "create_ticket"], settings=settings) == 0
    ticket = json.loads(capsys.readouterr().out)["task"]
    events = [e.event_type for e in build_runtime(settings).store.get_events(ticket["task_id"])]
    assert events == PRE_MIGRATION_TICKET_EVENTS
    assert ticket["retry_count"] == 1
    assert (ticket["verification_checks_passed"], ticket["verification_checks_total"]) == (6, 6)

    goal = "Update employee E-17 title to Senior Engineer and confirm the change in a note file."
    assert main(["--json", "run", goal], settings=settings) == 4
    paused = json.loads(capsys.readouterr().out)["task"]
    assert [(s["tool"], s["operation"]) for s in paused["steps"]] == [
        ("company_api", "update_employee"),
        ("file", "write"),
    ]
    assert paused["pending_action"]["risk"] == "write"
    assert paused["pending_action"]["reason"] == "Employee record mutation requires human approval."
    assert main(["--json", "approve", paused["task_id"], "--yes"], settings=settings) == 0
    done = json.loads(capsys.readouterr().out)["task"]
    assert done["status"] == "completed"
    assert (done["verification_checks_passed"], done["verification_checks_total"]) == (5, 5)
    state = build_runtime(settings).runtime.load(paused["task_id"])
    assert state.pending_action is None and state.approval_status.value == "none"
