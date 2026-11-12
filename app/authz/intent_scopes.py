"""Authorization scopes derived from trusted dynamic intents.

Scopes are independent of planner/LLM output. The LLM cannot create or expand them.
"""

from __future__ import annotations

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
)
from app.authz.origin import authorization_for_supported_goal, welcome_customer_ticket_scope
from app.llm.mock import parse_supported_goal
from app.planning.intents import TrustedIntent, parse_trusted_intent


def _attempts(max_recovery_attempts: int) -> int:
    return max(1, 1 + max_recovery_attempts)


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
