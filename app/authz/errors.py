"""Typed authorization failures with machine-readable reason codes."""

from __future__ import annotations

from enum import Enum
from typing import Any


class AuthorizationReason(str, Enum):
    """Stable reason codes recorded on failures and audit events."""

    PLAN_UNAUTHORIZED = "PLAN_UNAUTHORIZED"
    ACTION_UNAUTHORIZED = "ACTION_UNAUTHORIZED"
    OPERATION_UNAUTHORIZED = "OPERATION_UNAUTHORIZED"
    ENTITY_UNAUTHORIZED = "ENTITY_UNAUTHORIZED"
    FIELD_UNAUTHORIZED = "FIELD_UNAUTHORIZED"
    MISSING_AUTHORIZATION = "MISSING_AUTHORIZATION"
    MISSING_FINGERPRINT = "MISSING_FINGERPRINT"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    MALFORMED_FINGERPRINT = "MALFORMED_FINGERPRINT"
    MALFORMED_AUTHORIZATION = "MALFORMED_AUTHORIZATION"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    UNSUPPORTED_CONSTRAINT = "UNSUPPORTED_CONSTRAINT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REFERENCE_UNRESOLVED = "REFERENCE_UNRESOLVED"
    LEGACY_UNMAPPED = "LEGACY_UNMAPPED"


class AuthorizationError(ValueError):
    """Fail-closed authorization denial."""

    def __init__(
        self,
        reason: AuthorizationReason,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = dict(details or {})

    def audit_payload(self) -> dict[str, Any]:
        """Structured failure payload safe for audit events (no secrets)."""
        payload: dict[str, Any] = {
            "reason_code": self.reason.value,
            "message": self.message,
        }
        if self.details:
            # Only shallow, non-secret diagnostic fields.
            safe = {
                key: value
                for key, value in self.details.items()
                if key
                in {
                    "operation",
                    "argument",
                    "constraint_type",
                    "step_id",
                    "budget",
                    "limit",
                    "observed",
                    "allowed_operations",
                    "version",
                    "kind",
                    "fingerprint_present",
                }
            }
            if safe:
                payload["details"] = safe
        return payload
