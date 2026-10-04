"""Deterministic failure classification and bounded recovery policy."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.models.enums import FailureType
from app.tools.base import BaseTool
from app.tools.result import ToolResult


class RecoveryAction(str, Enum):
    RETRY = "retry"
    REPLAN = "replan"  # reserved for a later phase
    FAIL = "fail"


class RecoveryDecision(BaseModel):
    action: RecoveryAction
    reason: str
    allowed: bool
    failure_type: FailureType
    attempt: int
    max_recovery_attempts: int
    retries_used: int = 0


def classify_failure(result: ToolResult) -> FailureType:
    """Map a ToolResult into an existing FailureType without LLM involvement."""
    raw_type = (result.error_type or "").strip().lower()
    message = (result.error_message or "").strip().lower()
    blob = f"{raw_type} {message}"

    try:
        explicit = FailureType(raw_type)
        if explicit is FailureType.TRANSIENT:
            return FailureType.TRANSIENT
        if explicit is FailureType.INVALID_ARGS:
            return FailureType.INVALID_ARGS
        if explicit is FailureType.POLICY_BLOCKED:
            return FailureType.POLICY_BLOCKED
        if explicit is FailureType.MISSING_INFO:
            return FailureType.MISSING_INFO
        if explicit is FailureType.VERIFICATION_FAILED:
            return FailureType.VERIFICATION_FAILED
        if explicit is FailureType.REPEATED:
            return FailureType.REPEATED
    except ValueError:
        pass

    if any(
        token in blob
        for token in ("503", "timeout", "temporarily unavailable", "service unavailable", "transient")
    ):
        return FailureType.TRANSIENT

    if any(token in blob for token in ("invalid", "validation", "schema")):
        return FailureType.INVALID_ARGS

    if any(token in blob for token in ("not found", "missing", "unknown customer", "unknown employee")):
        return FailureType.MISSING_INFO

    if any(token in blob for token in ("permission", "denied", "forbidden", "policy")):
        return FailureType.POLICY_BLOCKED

    if "conflict" in blob:
        return FailureType.ENVIRONMENT_UNEXPECTED

    return FailureType.ENVIRONMENT_UNEXPECTED


def evaluate_recovery(
    *,
    failure_type: FailureType,
    tool: BaseTool | None,
    tool_args: dict[str, Any] | None,
    attempt: int,
    retries_used: int,
    max_recovery_attempts: int,
) -> RecoveryDecision:
    """Decide whether a failed attempt may be retried."""
    retry_safe = False
    if tool is not None:
        retry_safe = tool.is_retry_safe(tool_args)

    if failure_type is FailureType.TRANSIENT and retry_safe:
        if retries_used < max_recovery_attempts:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                reason=(
                    "Transient failure on a retry-safe operation with recovery budget remaining"
                ),
                allowed=True,
                failure_type=failure_type,
                attempt=attempt,
                max_recovery_attempts=max_recovery_attempts,
                retries_used=retries_used,
            )
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason="Transient failure but recovery budget exhausted",
            allowed=False,
            failure_type=FailureType.REPEATED,
            attempt=attempt,
            max_recovery_attempts=max_recovery_attempts,
            retries_used=retries_used,
        )

    if failure_type is FailureType.INVALID_ARGS:
        reason = "Validation/invalid-argument failures are not blindly retried"
    elif failure_type is FailureType.MISSING_INFO:
        reason = "Missing-information failures are not blindly retried"
    elif failure_type is FailureType.POLICY_BLOCKED:
        reason = "Permission/policy failures are not retried"
    elif failure_type is FailureType.TRANSIENT and not retry_safe:
        reason = "Transient failure but operation is not marked retry-safe"
    else:
        reason = f"Failure type '{failure_type.value}' is not eligible for automatic retry"

    return RecoveryDecision(
        action=RecoveryAction.FAIL,
        reason=reason,
        allowed=False,
        failure_type=failure_type,
        attempt=attempt,
        max_recovery_attempts=max_recovery_attempts,
        retries_used=retries_used,
    )
