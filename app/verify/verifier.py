"""Deterministic independent outcome verifier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.enums import PlanStepStatus, ToolCallStatus, VerificationStatus
from app.models.goal import SuccessCriterion
from app.models.records import EvidenceItem
from app.models.state import ExecutionState
from app.tools.file import is_protected_file
from app.verify import binding_checks, company_checks
from app.verify.results import VerificationCheckResult, VerificationResult
from app.verify.rules import collect_criteria, created_ticket_ids
from app.world.repository import EMPLOYEE_UPDATE_FIELDS, CompanyRepository

_CRITERION_KINDS = {
    "step_completed": "step",
    "ticket_exists": "ticket",
    "ticket": "ticket",
    "employee_field": "employee",
    "employee": "employee",
    "file_exists": "file",
    "file": "file",
    # v0.3.0 company operations; selected only by explicit criterion_type.
    **{name: name for name in company_checks.CHECKS},
    "reference_binding": "reference_binding",
}

# Every key of an expected_value must be one the check actually evaluates (or a
# provenance key); anything else would be silently ignored and is unsupported.
_EVALUABLE_KEYS: dict[str, frozenset[str]] = {
    "step": frozenset({"step_id", "tool", "operation"}),
    "ticket": frozenset(
        {
            "ticket_id",
            "customer_id",
            "company",
            "subject_contains",
            "description_contains",
            "text_contains",
            "source_call_id",
            "source_step_id",
        }
    ),
    "employee": frozenset({"employee_id"}) | EMPLOYEE_UPDATE_FIELDS,
    "file": frozenset({"path", "content_contains"}),
    **company_checks.EVALUABLE_KEYS,
    "reference_binding": binding_checks.EVALUABLE_KEYS,
}


def _unsupported(expected: Any, reason: str) -> VerificationCheckResult:
    return VerificationCheckResult(
        name="unsupported_criterion",
        passed=False,
        expected=expected,
        actual=None,
        message=f"Unsupported criterion: {reason}",
    )


class OutcomeVerifier:
    """Inspects actual company DB / workspace state. Never trusts ToolResult alone."""

    def __init__(
        self,
        repository: CompanyRepository,
        workspace_root: str | Path,
    ) -> None:
        self.repository = repository
        self.workspace_root = Path(workspace_root).resolve()

    def verify(self, state: ExecutionState) -> VerificationResult:
        criteria = collect_criteria(state)
        if not criteria:
            evidence = [
                EvidenceItem(
                    type="verification_vacuous",
                    description=(
                        "No success criteria or mutable outcomes to verify; "
                        "execution finished without outcome checks."
                    ),
                    data={"task_id": state.task_id},
                )
            ]
            # Nothing failed, so the task may complete, but it is reported as
            # unverified rather than as independently verified.
            return VerificationResult(
                status=VerificationStatus.UNVERIFIED,
                passed=True,
                summary="No verifiable outcomes; nothing was independently verified.",
                checks=[],
                evidence=evidence,
            )

        checks: list[VerificationCheckResult] = []
        evidence: list[EvidenceItem] = []
        for criterion in criteria:
            check, items = self._check_criterion(state, criterion)
            checks.append(check)
            evidence.extend(items)

        passed = all(c.passed for c in checks)
        if passed:
            summary = (
                f"All {len(checks)} verification check(s) passed against actual world state."
            )
            return VerificationResult(
                status=VerificationStatus.PASSED,
                passed=True,
                summary=summary,
                checks=checks,
                evidence=evidence,
            )

        failed = [c for c in checks if not c.passed]
        reason = "; ".join(c.message for c in failed)
        return VerificationResult(
            status=VerificationStatus.FAILED,
            passed=False,
            summary=f"Verification failed: {reason}",
            checks=checks,
            evidence=evidence,
            failure_reason=reason,
        )

    def _check_criterion(
        self,
        state: ExecutionState,
        criterion: SuccessCriterion,
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        ctype = (criterion.criterion_type or "generic").lower()
        expected = criterion.expected_value
        if not isinstance(expected, dict):
            expected = {"value": expected}

        kind = _CRITERION_KINDS.get(ctype)
        if kind is None:
            # Other types (including the default "generic") dispatch by keys.
            if any(k in expected for k in ("ticket_id", "customer_id", "subject_contains")):
                kind = "ticket"
            elif "employee_id" in expected:
                kind = "employee"
            elif "path" in expected:
                kind = "file"
        if kind is None:
            return _unsupported(
                expected,
                f"criterion_type {criterion.criterion_type!r} has no verification rule",
            ), []

        unknown = sorted(str(k) for k in expected if k not in _EVALUABLE_KEYS[kind])
        if unknown:
            return _unsupported(
                expected,
                f"{kind} criteria cannot check {', '.join(unknown)} "
                f"(checkable: {', '.join(sorted(_EVALUABLE_KEYS[kind]))})",
            ), []

        if kind == "step":
            return self._verify_step_completed(state, criterion, expected)
        if kind == "ticket":
            return self._verify_ticket(state, criterion, expected)
        if kind == "employee":
            return self._verify_employee(criterion, expected, require_field=ctype == "employee_field")
        if kind in company_checks.CHECKS:
            return company_checks.CHECKS[kind](self.repository, state, expected)
        if kind == "reference_binding":
            return binding_checks.verify_reference_binding(state, expected)
        return self._verify_file(criterion, expected)

    def _verify_step_completed(
        self,
        state: ExecutionState,
        criterion: SuccessCriterion,
        expected: dict[str, Any],
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        step_id = expected.get("step_id")
        step = next((s for s in state.plan if s.step_id == step_id), None)
        succeeded = any(
            c.step_id == step_id and c.status is ToolCallStatus.SUCCEEDED
            for c in state.tool_calls
        )
        if step is None or step.status is not PlanStepStatus.COMPLETED or not succeeded:
            check = VerificationCheckResult(
                name="step_completed",
                passed=False,
                expected=expected,
                actual={
                    "step_status": step.status.value if step else None,
                    "successful_tool_call": succeeded,
                },
                message=f"Planned mutating step '{step_id}' was not completed",
            )
            return check, []

        actual_action = {
            "tool": step.resolved_tool_name(),
            "operation": step.arguments.get("operation"),
        }
        mismatched = sorted(
            key for key in ("tool", "operation") if key in expected and expected[key] != actual_action[key]
        )
        if mismatched:
            check = VerificationCheckResult(
                name="step_completed",
                passed=False,
                expected=expected,
                actual={"step_status": step.status.value, **actual_action},
                message=f"Step '{step_id}' ran a different action than expected ({', '.join(mismatched)})",
            )
            return check, []

        call = next(
            c
            for c in reversed(state.tool_calls)
            if c.step_id == step_id and c.status is ToolCallStatus.SUCCEEDED
        )
        problem = binding_checks.approval_problem(call)
        if problem:
            check = VerificationCheckResult(
                name="step_completed",
                passed=False,
                expected=expected,
                actual={"step_status": step.status.value, "approval_fingerprint": call.approval_fingerprint},
                message=f"Step '{step_id}' completed but {problem}",
            )
            return check, []

        actual: dict[str, Any] = {"step_status": step.status.value, "successful_tool_call": True}
        if call.approval_required:
            actual["approval_fingerprint"] = call.approval_fingerprint
        check = VerificationCheckResult(
            name="step_completed",
            passed=True,
            expected=expected,
            actual=actual,
            message=f"Planned step '{step_id}' completed with a successful tool call",
        )
        return check, []

    def _verify_ticket(
        self,
        state: ExecutionState,
        criterion: SuccessCriterion,
        expected: dict[str, Any],
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        """Verify one exact ticket; never fall back to another ticket.

        - ``ticket_id`` given: that exact ticket must exist and match.
        - Bound to a creation call (``source_call_id``) without an ID: the creation
          result was missing/malformed, so verification fails.
        - No ``ticket_id``: a creation requirement; only tickets created by a
          successful create_ticket call in this task can satisfy it.
        """
        ticket_id = expected.get("ticket_id")
        if ticket_id:
            return self._verify_ticket_by_id(str(ticket_id), expected)

        if "source_call_id" in expected:
            check = VerificationCheckResult(
                name="ticket_exists",
                passed=False,
                expected=expected,
                actual=None,
                message=(
                    "create_ticket call result has no ticket_id "
                    f"(call_id={expected.get('source_call_id')})"
                ),
            )
            return check, []

        candidates = created_ticket_ids(state)
        if not candidates:
            check = VerificationCheckResult(
                name="ticket_created",
                passed=False,
                expected=expected,
                actual={"created_ticket_ids": []},
                message=(
                    "No ticket was created by a successful create_ticket call in this task"
                ),
            )
            return check, []

        failure: tuple[VerificationCheckResult, list[EvidenceItem]] | None = None
        for candidate in candidates:
            check, evidence = self._verify_ticket_by_id(candidate, expected)
            if check.passed:
                return check, evidence
            failure = failure or (check, evidence)
        assert failure is not None
        return failure

    def _verify_ticket_by_id(
        self,
        ticket_id: str,
        expected: dict[str, Any],
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        customer_id = expected.get("customer_id")
        company = expected.get("company")

        ticket = self.repository.get_ticket(ticket_id)
        evidence: list[EvidenceItem] = []
        name = "ticket_exists"
        if ticket is None:
            check = VerificationCheckResult(
                name=name,
                passed=False,
                expected=expected,
                actual=None,
                message=f"Ticket not found in company database (ticket_id={ticket_id})",
            )
            return check, evidence

        evidence.append(
            EvidenceItem(
                type="ticket_record",
                description=f"Ticket {ticket.ticket_id} exists in company database",
                data=ticket.model_dump(mode="json"),
            )
        )

        # Customer match
        if customer_id and ticket.customer_id != customer_id:
            check = VerificationCheckResult(
                name="customer_matches",
                passed=False,
                expected={"customer_id": customer_id},
                actual={"customer_id": ticket.customer_id},
                message=(
                    f"Ticket customer mismatch: expected {customer_id}, "
                    f"got {ticket.customer_id}"
                ),
            )
            return check, evidence

        if company:
            customers = self.repository.find_customers(customer_id=ticket.customer_id)
            actual_company = customers[0].company if customers else None
            evidence.append(
                EvidenceItem(
                    type="customer_record",
                    description=f"Ticket customer company observed as {actual_company}",
                    data={
                        "customer_id": ticket.customer_id,
                        "company": actual_company,
                    },
                )
            )
            if actual_company != company:
                check = VerificationCheckResult(
                    name="customer_matches",
                    passed=False,
                    expected={"company": company},
                    actual={"company": actual_company},
                    message=(
                        f"Ticket company mismatch: expected {company}, "
                        f"got {actual_company}"
                    ),
                )
                return check, evidence

        text_blob = f"{ticket.subject}\n{ticket.description}".lower()
        field_map = {
            "subject_contains": ticket.subject.lower(),
            "description_contains": ticket.description.lower(),
            "text_contains": text_blob,
        }
        for key, haystack in field_map.items():
            needle = expected.get(key)
            if not needle:
                continue
            if str(needle).lower() not in haystack:
                check = VerificationCheckResult(
                    name="details_match",
                    passed=False,
                    expected={key: needle},
                    actual={
                        "subject": ticket.subject,
                        "description": ticket.description,
                    },
                    message=f"Ticket details missing expected {key}={needle!r}",
                )
                return check, evidence

        evidence.append(
            EvidenceItem(
                type="ticket_details",
                description=(
                    f"Ticket {ticket.ticket_id} matches expected customer/details"
                ),
                data={
                    "ticket_id": ticket.ticket_id,
                    "customer_id": ticket.customer_id,
                    "subject": ticket.subject,
                    "description": ticket.description,
                },
            )
        )
        check = VerificationCheckResult(
            name=name,
            passed=True,
            expected=expected,
            actual=ticket.model_dump(mode="json"),
            message=f"Ticket {ticket.ticket_id} verified in company database",
        )
        return check, evidence

    def _verify_employee(
        self,
        criterion: SuccessCriterion,
        expected: dict[str, Any],
        *,
        require_field: bool,
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        fields = sorted(key for key in expected if key in EMPLOYEE_UPDATE_FIELDS)
        if require_field and not fields:
            return _unsupported(expected, "employee_field criterion names no field to compare"), []
        employee_id = expected.get("employee_id")
        employee = (
            self.repository.get_employee(str(employee_id)) if employee_id else None
        )
        if employee is None:
            check = VerificationCheckResult(
                name="employee_exists",
                passed=False,
                expected=expected,
                actual=None,
                message=f"Employee not found: {employee_id}",
            )
            return check, []

        mismatches: dict[str, Any] = {}
        for field in fields:
            actual = getattr(employee, field)
            if actual != expected[field]:
                mismatches[field] = {"expected": expected[field], "actual": actual}

        evidence = [
            EvidenceItem(
                type="employee_record",
                description=f"Employee {employee.employee_id} observed in company database",
                data=employee.model_dump(mode="json"),
            )
        ]
        if mismatches:
            check = VerificationCheckResult(
                name="employee_field",
                passed=False,
                expected=expected,
                actual=employee.model_dump(mode="json"),
                message=f"Employee field mismatch: {mismatches}",
            )
            return check, evidence

        check = VerificationCheckResult(
            name="employee_field" if fields else "employee_exists",
            passed=True,
            expected=expected,
            actual=employee.model_dump(mode="json"),
            message=(
                f"Employee {employee.employee_id} fields verified: {', '.join(fields)}"
                if fields
                else f"Employee {employee.employee_id} exists in company database"
            ),
        )
        return check, evidence

    def _verify_file(
        self,
        criterion: SuccessCriterion,
        expected: dict[str, Any],
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        rel = expected.get("path")
        if not rel:
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                message="file_exists criterion missing path",
            )
            return check, []

        candidate = Path(str(rel))
        if candidate.is_absolute() or ".." in candidate.parts:
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                message=f"Unsafe file path rejected by verifier: {rel}",
            )
            return check, []

        path = (self.workspace_root / candidate).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                message=f"Path escapes workspace: {rel}",
            )
            return check, []

        if not path.exists() or not path.is_file():
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                actual={"exists": False, "path": str(rel)},
                message=f"Expected file missing: {rel}",
            )
            return check, []

        if is_protected_file(path):
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                message=f"Database files are not valid workspace outcomes: {rel}",
            )
            return check, []

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            check = VerificationCheckResult(
                name="file_exists",
                passed=False,
                expected=expected,
                message=f"File is not readable UTF-8 text: {rel}",
            )
            return check, []
        evidence = [
            EvidenceItem(
                type="file_record",
                description=f"File '{rel}' exists and is readable in workspace",
                data={"path": str(rel), "bytes": len(content.encode("utf-8"))},
            )
        ]
        needle = expected.get("content_contains")
        if needle is not None and str(needle) not in content:
            check = VerificationCheckResult(
                name="file_content",
                passed=False,
                expected={"path": rel, "content_contains": needle},
                actual={"path": rel, "content_preview": content[:200]},
                message=f"File content missing expected text for {rel}",
            )
            return check, evidence

        if needle is not None:
            evidence.append(
                EvidenceItem(
                    type="file_content",
                    description=f"File '{rel}' contains expected confirmation text",
                    data={"path": str(rel), "matched": True},
                )
            )
        check = VerificationCheckResult(
            name="file_exists",
            passed=True,
            expected=expected,
            actual={"path": str(rel), "exists": True},
            message=f"File '{rel}' verified in workspace",
        )
        return check, evidence
