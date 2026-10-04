"""Lifecycle and classification enums for the runtime domain."""

from enum import Enum


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class ApprovalStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureType(str, Enum):
    TRANSIENT = "transient"
    INVALID_ARGS = "invalid_args"
    MISSING_INFO = "missing_info"
    ENVIRONMENT_UNEXPECTED = "environment_unexpected"
    POLICY_BLOCKED = "policy_blocked"
    VERIFICATION_FAILED = "verification_failed"
    REPEATED = "repeated"


class Actor(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"
