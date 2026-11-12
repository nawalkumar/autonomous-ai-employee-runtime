"""Authorization scopes derived from trusted dynamic intents.

Scopes are independent of planner/LLM output. The LLM cannot create or expand them.
"""

from __future__ import annotations

import re

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    AllowedValuesConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromCollectionConstraint,
    FromStepOutputConstraint,
)
from app.authz.origin import authorization_for_supported_goal, welcome_customer_ticket_scope
from app.llm.mock import parse_supported_goal
from app.planning.intents import GOAL_COLLECTION_MAX, TrustedIntent, parse_trusted_intent
from app.world.models import (
    EMPLOYEE_ID_PATTERN,
    OPEN_TICKET_STATUSES,
    TICKET_ID_PATTERN,
    TICKET_TRANSITIONS,
)


def _attempts(max_recovery_attempts: int) -> int:
    return max(1, 1 + max_recovery_attempts)


_EMPLOYEE_ID = re.compile(EMPLOYEE_ID_PATTERN)
_TICKET_ID = re.compile(TICKET_ID_PATTERN)
_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
_STATUSES = frozenset(TICKET_TRANSITIONS)
_TRANSITION_TARGETS = frozenset().union(*TICKET_TRANSITIONS.values())


def _budgets(steps: int, mutations: int, calls: int) -> AuthorizationBudgets:
    return AuthorizationBudgets(
        max_steps=min(100, max(1, steps)),
        max_mutations=min(100, max(0, mutations)),
        max_tool_calls=min(500, max(1, calls)),
    )


def _require_bound(entities: dict) -> int:
    value = entities.get("bound")
    if type(value) is not int or not 1 <= value <= GOAL_COLLECTION_MAX:
        raise AuthorizationError(
            AuthorizationReason.LEGACY_UNMAPPED,
            "Collection bound is not an explicit authorized limit.",
        )
    return value


def _ticket_filters(entities: dict, *, assignable_only: bool) -> dict[str, str]:
    status = str(entities.get("status") or "")
    priority = str(entities.get("priority") or "")
    allowed = OPEN_TICKET_STATUSES if assignable_only else _STATUSES
    if status not in allowed or priority not in _PRIORITIES:
        raise AuthorizationError(
            AuthorizationReason.LEGACY_UNMAPPED,
            "Ticket filters are not an authorized status and priority.",
        )
    return {"status": status, "priority": priority}


def authorization_for_trusted_intent(
    intent: TrustedIntent,
    *,
    max_recovery_attempts: int = 2,
) -> AuthorizationScope:
    """Narrow scope for a catalog intent. Fail closed on unknown intent ids."""
    attempts = _attempts(max_recovery_attempts)
    entities = intent.entities

    if intent.intent_id == "list_open_high_priority_tickets":
        return AuthorizationScope(
            allowed_operations=["company_api.list_tickets"],
            constraints={
                "company_api.list_tickets": {
                    "filters": ExactValueConstraint(
                        value={"status": "open", "priority": "high"}
                    ),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=0,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "list_tickets_for_customer":
        customer_id = str(entities["customer_id"])
        return AuthorizationScope(
            allowed_operations=["company_api.list_tickets"],
            constraints={
                "company_api.list_tickets": {
                    "filters": ExactValueConstraint(value={"customer_id": customer_id}),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=0,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "create_ticket_for_customer":
        customer_id = str(entities["customer_id"])
        return AuthorizationScope(
            allowed_operations=["company_api.create_ticket"],
            constraints={
                "company_api.create_ticket": {
                    "customer_id": ExactValueConstraint(value=customer_id),
                    "subject": ExactValueConstraint(value=str(entities["subject"])),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=1 * attempts,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "update_ticket_status":
        ticket_id = str(entities["ticket_id"])
        status = str(entities["status"])
        return AuthorizationScope(
            allowed_operations=["company_api.update_ticket"],
            constraints={
                "company_api.update_ticket": {
                    "ticket_id": ExactValueConstraint(value=ticket_id),
                    "fields": FieldKeysConstraint(allowed_keys=["status"]),
                    "fields.status": ExactValueConstraint(value=status),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=1 * attempts,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "assign_ticket":
        ticket_id = str(entities["ticket_id"])
        employee_id = str(entities["employee_id"])
        return AuthorizationScope(
            allowed_operations=["company_api.assign_ticket"],
            constraints={
                "company_api.assign_ticket": {
                    "ticket_id": ExactValueConstraint(value=ticket_id),
                    "employee_id": ExactValueConstraint(value=employee_id),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=1 * attempts,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "update_employee_title":
        employee_id = str(entities["employee_id"])
        title = str(entities["title"])
        return AuthorizationScope(
            allowed_operations=["company_api.update_employee"],
            constraints={
                "company_api.update_employee": {
                    "employee_id": ExactValueConstraint(value=employee_id),
                    "fields": FieldKeysConstraint(allowed_keys=["title"]),
                    "fields.title": ExactValueConstraint(value=title),
                }
            },
            budgets=AuthorizationBudgets(
                max_steps=1,
                max_mutations=1 * attempts,
                max_tool_calls=1 * attempts,
            ),
        )

    if intent.intent_id == "create_customer_and_welcome_ticket":
        create_step_id = str(entities.get("create_step_id") or "create_customer")
        name = str(entities["name"])
        company = str(entities["company"])
        email = str(entities["email"])
        scope = welcome_customer_ticket_scope(
            create_step_id=create_step_id,
            max_recovery_attempts=max_recovery_attempts,
        )
        # Narrow create_customer identity to the trusted parse; ticket customer_id
        # remains a from_step_output binding to the authorized producer step.
        constraints = dict(scope.constraints)
        create_constraints = dict(constraints.get("company_api.create_customer") or {})
        create_constraints.update(
            {
                "name": ExactValueConstraint(value=name),
                "company": ExactValueConstraint(value=company),
                "email": ExactValueConstraint(value=email),
            }
        )
        constraints["company_api.create_customer"] = create_constraints
        ticket_constraints = dict(constraints.get("company_api.create_ticket") or {})
        ticket_constraints.setdefault(
            "customer_id",
            FromStepOutputConstraint(
                step_id=create_step_id,
                path=["customer", "customer_id"],
            ),
        )
        subject = entities.get("subject")
        if subject is not None:
            ticket_constraints["subject"] = ExactValueConstraint(value=str(subject))
        constraints["company_api.create_ticket"] = ticket_constraints
        return scope.model_copy(update={"constraints": constraints})

    if intent.intent_id == "assign_bounded_tickets":
        bound = _require_bound(entities)
        employee_id = str(entities.get("employee_id") or "")
        if _EMPLOYEE_ID.fullmatch(employee_id) is None:
            raise AuthorizationError(
                AuthorizationReason.LEGACY_UNMAPPED,
                "Assignment requires an explicit employee id.",
            )
        filters = _ticket_filters(entities, assignable_only=True)
        source_step = str(entities.get("list_step_id") or "list")
        return AuthorizationScope(
            allowed_operations=[
                "company_api.list_tickets",
                "company_api.assign_ticket",
            ],
            constraints={
                "company_api.list_tickets": {
                    "filters": ExactValueConstraint(value=filters),
                    "limit": ExactValueConstraint(value=bound),
                    "offset": ExactValueConstraint(value=0),
                },
                "company_api.assign_ticket": {
                    "employee_id": ExactValueConstraint(value=employee_id),
                    "ticket_id": FromCollectionConstraint(source_step_id=source_step),
                },
            },
            budgets=_budgets(
                steps=1 + bound,
                mutations=bound * attempts,
                calls=(1 + bound) * attempts,
            ),
        )

    if intent.intent_id == "list_bounded_tickets":
        bound = _require_bound(entities)
        filters = _ticket_filters(entities, assignable_only=False)
        return AuthorizationScope(
            allowed_operations=["company_api.list_tickets"],
            constraints={
                "company_api.list_tickets": {
                    "filters": ExactValueConstraint(value=filters),
                    "limit": ExactValueConstraint(value=bound),
                    "offset": ExactValueConstraint(value=0),
                }
            },
            budgets=_budgets(steps=1, mutations=0, calls=1 * attempts),
        )

    if intent.intent_id == "update_ticket_status_batch":
        status = str(entities.get("status") or "")
        raw_ids = entities.get("ticket_ids")
        if status not in _TRANSITION_TARGETS or not isinstance(raw_ids, list):
            raise AuthorizationError(
                AuthorizationReason.LEGACY_UNMAPPED,
                "Batch status update is not an authorized transition.",
            )
        ticket_ids = [str(item) for item in raw_ids]
        if (
            len(ticket_ids) < 2
            or len(ticket_ids) > GOAL_COLLECTION_MAX
            or len(ticket_ids) != len(set(ticket_ids))
            or any(_TICKET_ID.fullmatch(ticket_id) is None for ticket_id in ticket_ids)
        ):
            raise AuthorizationError(
                AuthorizationReason.LEGACY_UNMAPPED,
                "Batch status update requires explicit unique ticket ids.",
            )
        count = len(ticket_ids)
        return AuthorizationScope(
            allowed_operations=["company_api.update_ticket"],
            constraints={
                "company_api.update_ticket": {
                    "ticket_id": AllowedValuesConstraint(values=ticket_ids),
                    "fields": FieldKeysConstraint(allowed_keys=["status"]),
                    "fields.status": ExactValueConstraint(value=status),
                }
            },
            budgets=_budgets(
                steps=count,
                mutations=count * attempts,
                calls=count * attempts,
            ),
        )

    raise AuthorizationError(
        AuthorizationReason.LEGACY_UNMAPPED,
        f"No authorization scope for intent {intent.intent_id!r}.",
    )


def authorization_for_goal(
    goal_text: str,
    *,
    dynamic_planning_enabled: bool = False,
    max_recovery_attempts: int = 2,
) -> AuthorizationScope:
    """Trusted scope for a goal text. Mock scenarios always win; dynamic intents
    only when the feature flag is on. Never accepts caller-supplied scopes.
    """
    if parse_supported_goal(goal_text) is not None:
        return authorization_for_supported_goal(
            goal_text, max_recovery_attempts=max_recovery_attempts
        )
    if dynamic_planning_enabled:
        intent = parse_trusted_intent(goal_text)
        if intent is not None:
            return authorization_for_trusted_intent(
                intent, max_recovery_attempts=max_recovery_attempts
            )
    raise AuthorizationError(
        AuthorizationReason.LEGACY_UNMAPPED,
        "Goal is not a supported deterministic contract; no authorization granted.",
    )
