"""Goal-scoped plan authorization: what a task is permitted to do.

Distinct from the HITL approval policy (which asks a human whether a gated
action should run) and from independent verification (which checks what
actually happened). Authorization is granted at a trusted task-creation
boundary, persisted with the task, and rechecked before every tool attempt.
"""

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    AllowedValuesConstraint,
    ArgumentConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
    IntRangeConstraint,
    parse_authorization_scope,
)
from app.authz.origin import (
    authorization_for_supported_goal,
    legacy_authorization_for_state,
    welcome_customer_ticket_scope,
)
from app.authz.plan_check import authorize_plan
from app.authz.runtime_check import (
    authorize_action,
    ensure_task_authorization,
    validate_authorization_fingerprint,
)

__all__ = [
    "AllowedValuesConstraint",
    "ArgumentConstraint",
    "AuthorizationBudgets",
    "AuthorizationError",
    "AuthorizationReason",
    "AuthorizationScope",
    "ExactValueConstraint",
    "FieldKeysConstraint",
    "FromStepOutputConstraint",
    "IntRangeConstraint",
    "authorize_action",
    "authorize_plan",
    "authorization_for_supported_goal",
    "ensure_task_authorization",
    "legacy_authorization_for_state",
    "parse_authorization_scope",
    "validate_authorization_fingerprint",
    "welcome_customer_ticket_scope",
]
