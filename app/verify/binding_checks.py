"""Independent checks for resolved step-output references and approvals.

A reference_binding check proves that the value a step actually ran with (its
persisted, resolved tool-call arguments) equals the value the referenced step
actually produced (its persisted successful output). The checks of the records
themselves (for example ticket_exists comparing the stored ticket's customer
with the resolved customer_id) then tie the database rows together.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import ToolCallStatus
from app.models.fingerprint import action_fingerprint
from app.models.records import EvidenceItem, ToolCallRecord
from app.models.references import BindingError, location_text, producer_output, read_path
from app.models.state import ExecutionState
from app.policy import ApprovalPolicy
from app.verify.results import VerificationCheckResult

EVALUABLE_KEYS = frozenset(
    {"source_call_id", "source_step_id", "location", "producer_step_id", "path"}
)


def _fail(expected: Any, message: str, actual: Any = None) -> VerificationCheckResult:
    return VerificationCheckResult(
        name="reference_binding", passed=False, expected=expected, actual=actual, message=message
    )


def verify_reference_binding(
    state: ExecutionState, expected: dict[str, Any]
) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
    location, path = expected.get("location"), expected.get("path")
    producer = expected.get("producer_step_id")
    if not isinstance(location, list) or not location or not isinstance(path, list) or not path:
        return VerificationCheckResult(
            name="unsupported_criterion", passed=False, expected=expected,
            message="Unsupported criterion: reference_binding needs location and path",
        ), []
    call = next((c for c in state.tool_calls if c.call_id == expected.get("source_call_id")), None)
    if call is None or call.status is not ToolCallStatus.SUCCEEDED:
        return _fail(expected, "The consuming call did not succeed in this task"), []
    try:
        used = read_path(call.arguments, tuple(location), f"Arguments of '{call.step_id}'")
        produced = read_path(
            producer_output(state, str(producer)), tuple(path), f"Output of '{producer}'"
        )
    except BindingError as exc:
        return _fail(expected, exc.message), []
    actual = {"used": used, "produced": produced}
    evidence = [
        EvidenceItem(
            type="reference_binding",
            description=(
                f"{call.step_id}.{location_text(tuple(location))} came from "
                f"{producer}.{location_text(tuple(path))}"
            ),
            data={**expected, **actual},
        )
    ]
    if used != produced:
        return _fail(
            expected,
            f"Step '{call.step_id}' ran with {location_text(tuple(location))}={used!r}, but "
            f"step '{producer}' produced {produced!r}",
            actual,
        ), evidence
    return VerificationCheckResult(
        name="reference_binding",
        passed=True,
        expected=expected,
        actual=actual,
        message=f"Step '{call.step_id}' used {used!r} produced by step '{producer}'",
    ), evidence


def approval_problem(call: ToolCallRecord) -> str | None:
    """Why a successful call's approval does not cover its exact action, or None.

    Calls recorded before v0.3.0 carry no approval information and are not judged.
    """
    if call.approval_required is None:
        return None
    decision = ApprovalPolicy().evaluate(tool_name=call.tool_name, arguments=call.arguments or {})
    if not decision.requires_approval:
        return None
    if call.approval_fingerprint != action_fingerprint(call.tool_name, call.arguments or {}):
        return "it ran without an approval bound to its exact action"
    return None
