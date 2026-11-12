"""Map a tool failure onto a recoverable replan trigger, or nothing.

Only business-state conflicts with a stable message are recoverable. Authorization
denials, approval rejection, budget exhaustion, policy refusals, transient
outages, malformed arguments and verification failures are not.
"""

from __future__ import annotations

import re

from app.models.enums import FailureType
from app.runtime.replan import ReplanTrigger
from app.tools.result import ToolResult

_TRANSITION = re.compile(r"cannot change from \w+ to \w+", re.IGNORECASE)
_NOT_OPEN = re.compile(
    r"only open or in-progress tickets can be assigned",
    re.IGNORECASE,
)
_TICKET_MISSING = re.compile(r"^Ticket not found:", re.IGNORECASE)


def recoverable_trigger(
    result: ToolResult,
    failure_type: FailureType,
) -> ReplanTrigger | None:
    """The supported trigger for this failure, or None when replanning is forbidden."""
    if failure_type in {
        FailureType.UNAUTHORIZED,
        FailureType.VERIFICATION_FAILED,
        FailureType.REPEATED,
        FailureType.TRANSIENT,
        FailureType.MISSING_INFO,
    }:
        return None
    message = (result.error_message or "").strip()
    if failure_type is FailureType.INVALID_ARGS and _TRANSITION.search(message):
        return ReplanTrigger.TICKET_STATE_CONFLICT
    if failure_type is FailureType.POLICY_BLOCKED and _NOT_OPEN.search(message):
        return ReplanTrigger.TICKET_STATE_CONFLICT
    if (
        failure_type is FailureType.ENVIRONMENT_UNEXPECTED
        and _TICKET_MISSING.search(message)
    ):
        return ReplanTrigger.ENTITY_MISSING_TRANSIENT
    return None
