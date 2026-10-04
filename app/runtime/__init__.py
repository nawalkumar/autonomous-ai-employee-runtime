"""Deterministic execution runtime with recovery, verification, and HITL."""

from app.runtime.executor import ApprovalError, ExecutionRuntime
from app.runtime.recovery import RecoveryAction, RecoveryDecision, classify_failure
from app.runtime.transitions import EventType, idempotency_key_for

__all__ = [
    "ApprovalError",
    "EventType",
    "ExecutionRuntime",
    "RecoveryAction",
    "RecoveryDecision",
    "classify_failure",
    "idempotency_key_for",
]
