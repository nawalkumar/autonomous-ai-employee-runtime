"""Deterministic execution runtime with recovery, verification, and HITL."""

from app.runtime.executor import ApprovalError, ExecutionRuntime, StaleApprovalError
from app.runtime.recovery import RecoveryAction, RecoveryDecision, classify_failure
from app.runtime.transitions import (
    EventType,
    action_fingerprint,
    idempotency_key_for,
    pending_fingerprint,
)

__all__ = [
    "ApprovalError",
    "EventType",
    "ExecutionRuntime",
    "RecoveryAction",
    "RecoveryDecision",
    "StaleApprovalError",
    "action_fingerprint",
    "classify_failure",
    "idempotency_key_for",
    "pending_fingerprint",
]
