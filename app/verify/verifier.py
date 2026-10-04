"""Deterministic independent outcome verifier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.enums import VerificationStatus
from app.models.goal import SuccessCriterion
from app.models.records import EvidenceItem
from app.models.state import ExecutionState
from app.verify.results import VerificationCheckResult, VerificationResult
from app.verify.rules import collect_criteria
from app.world.repository import CompanyRepository


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
            return VerificationResult(
                status=VerificationStatus.PASSED,
                passed=True,
                summary="No verifiable outcomes required.",
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

        if ctype in {"ticket_exists", "ticket"}:
            return self._verify_ticket(state, criterion, expected)
        if ctype in {"employee_field", "employee"}:
            return self._verify_employee(criterion, expected)
        if ctype in {"file_exists", "file"}:
            return self._verify_file(criterion, expected)
        # generic: dispatch by keys
        if any(k in expected for k in ("ticket_id", "customer_id", "subject_contains")):
            return self._verify_ticket(state, criterion, expected)
        if "employee_id" in expected:
            return self._verify_employee(criterion, expected)
        if "path" in expected:
            return self._verify_file(criterion, expected)
        check = VerificationCheckResult(
            name=criterion.description or "unsupported_criterion",
            passed=False,
            expected=expected,
            message=f"Unsupported criterion_type: {criterion.criterion_type}",
        )
        return check, []

    def _verify_ticket(
        self,
        state: ExecutionState,
        criterion: SuccessCriterion,
        expected: dict[str, Any],
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
        ticket_id = expected.get("ticket_id") or state.extracted_information.get(
            "last_ticket_id"
        )
        customer_id = expected.get("customer_id")
        company = expected.get("company")

        ticket = self.repository.get_ticket(str(ticket_id)) if ticket_id else None
        if ticket is None and customer_id:
            # Fall back to latest ticket for customer.
            matches = [
                t
                for t in self.repository.list_tickets()
                if t.customer_id == customer_id
            ]
            ticket = matches[-1] if matches else None

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
    ) -> tuple[VerificationCheckResult, list[EvidenceItem]]:
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
        for field in ("title", "name", "email", "department"):
            if field in expected and expected[field] is not None:
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
            name="employee_field",
            passed=True,
            expected=expected,
            actual=employee.model_dump(mode="json"),
            message=f"Employee {employee.employee_id} fields verified",
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

        content = path.read_text(encoding="utf-8")
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
