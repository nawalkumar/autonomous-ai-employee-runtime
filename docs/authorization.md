# Goal-scoped plan authorization

Developer notes for Milestone 4. The user-facing summary lives in the
[README](../README.md#goal-scoped-plan-authorization).

## Modules

| Module | Role |
|--------|------|
| `app/authz/models.py` | Versioned `AuthorizationScope`, typed constraints, budgets, parse/fail-closed |
| `app/authz/origin.py` | Trusted derivation from mock goals; W8 `from_step_output` helper; legacy mapping |
| `tests/authz_support.py` | Test-only `matching_scope_for_plan` (never imported by production) |
| `app/authz/plan_check.py` | Plan-time authorization (`authorize_plan`) |
| `app/authz/runtime_check.py` | Post-binding authorization (`authorize_action`, `ensure_task_authorization`) |
| `app/authz/errors.py` | `AuthorizationError` + stable `AuthorizationReason` codes |

## Enforcement points

1. **CLI / API task creation** — `authorization_for_supported_goal(goal)` then
   `ExecutionRuntime.create_task(..., authorization=scope)`.
2. **`create_task`** — requires a scope; runs `authorize_plan`; persists scope +
   fingerprint; emits `authorization.bound`.
3. **`_execute_step`** — after `prepare_arguments`, before the approval gate and again
   before every tool attempt; denials emit `authorization.denied` and fail with
   `FailureType.UNAUTHORIZED`.
4. **`approve_task`** — re-authorizes the resolved pending action; out-of-scope
   actions cannot be approved.

## Reason codes

`PLAN_UNAUTHORIZED`, `ACTION_UNAUTHORIZED`, `MISSING_AUTHORIZATION`,
`MALFORMED_AUTHORIZATION`, `UNKNOWN_OPERATION`, `UNSUPPORTED_CONSTRAINT`,
`BUDGET_EXCEEDED`, `REFERENCE_UNRESOLVED`, `LEGACY_UNMAPPED`.

## Tests

`tests/test_authorization.py` covers acceptance scenarios A–I and adversarial cases
(missing/malformed scope, unknown ops, constraint types, planner expansion, `$ref`
escape, restart/corruption, budgets, approval override, cross-task refs, demo
isolation).
