"""Bounded replanning of remaining work.

The replanner may only propose replacements for unfinished steps when a
recognized recoverable business-state condition occurs, and only when a trusted
enablement context says replanning is on. It never rewrites history, resets
budgets, expands authorization, or auto-approves mutations.

Execution does not call this module. ``AAE_REPLANNING_ENABLED=false`` is the
default, and a direct call with no enablement context fails closed.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.authz.errors import AuthorizationError
from app.authz.models import AuthorizationScope
from app.authz.plan_check import authorize_plan
from app.config import Settings, get_settings
from app.llm.base import LLMClient, LLMMessage, LLMRequest
from app.llm.errors import LLMProviderError
from app.llm.structured import StructuredOutputError, complete_structured
from app.models.enums import ApprovalStatus, PlanStepStatus
from app.models.plan import PlanStep
from app.models.records import ToolCallRecord
from app.models.state import ExecutionState
from app.planning.schemas import DynamicPlanDraft
from app.planning.validator import PlanValidationError, validate_plan
from app.runtime.collections import CollectionError, select_collection_items
from app.runtime.transitions import EventType, make_event
from app.store.sqlite_store import SQLiteStore
from app.tools.capabilities import CAPABILITIES, CapabilityRegistry, operation_of
from app.tools.registry import ToolRegistry

AuthorizationOutcome = Literal["authorized", "denied", "not_evaluated"]

# Reasons that refuse the attempt without consuming a replan round.
_NON_CONSUMING_PREFIXES = ("non_replanable:", "unknown_trigger:")
_NON_CONSUMING_REASONS = frozenset({"replanning_disabled", "replan_limit_exhausted"})

_EVENT_RESULT_KEYS = frozenset(
    {
        "task_id",
        "revision",
        "trigger",
        "previous_plan_id",
        "proposed_plan_id",
        "accepted_plan_id",
        "authorization",
        "budgets",
        "outcome",
    }
)


class ReplanTrigger(str, Enum):
    """Recognized recoverable conditions. Everything else is non-replanable."""

    TICKET_STATE_CONFLICT = "ticket_state_conflict"
    ENTITY_MISSING_TRANSIENT = "entity_missing_transient"


# Denials and security failures — never replan.
NON_REPLANABLE = frozenset(
    {
        "authorization_denial",
        "missing_authorization",
        "approval_rejection",
        "budget_exhaustion",
        "invalid_operation",
        "malformed_plan",
        "policy_violation",
        "unknown_verification",
        "ambiguous_entity",
    }
)


class ReplanEnablement(BaseModel):
    """Trusted opt-in for one replanning call.

    Build this from validated ``Settings`` (``from_settings``). A missing
    context, or ``enabled=False``, fails closed. Task payloads and model output
    cannot construct this object by themselves.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    max_rounds: int = Field(ge=0, le=5)

    @classmethod
    def from_settings(cls, settings: Settings) -> ReplanEnablement:
        return cls(
            enabled=bool(settings.aae_replanning_enabled),
            max_rounds=int(settings.aae_max_replan_rounds),
        )


def enablement_for_runtime(settings: Settings | None = None) -> ReplanEnablement:
    """Enablement implied by process settings. Disabled unless the flag is set."""
    return ReplanEnablement.from_settings(settings or get_settings())


class ReplanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    trigger: ReplanTrigger | None = None
    reason: str = ""
    remaining_step_ids: list[str] = Field(default_factory=list)


class ReplanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    revision: int
    trigger: str
    reason: str
    replacement_steps: list[PlanStep] = Field(default_factory=list)
    prior_plan_step_ids: list[str] = Field(default_factory=list)
    # Identity of a proposal that was not installed. Argument values are not stored.
    proposed_plan_id: str | None = None


class RevisionVerdict(BaseModel):
    """Budget and authorization outcome for one proposed replacement."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str
    authorization: AuthorizationOutcome
    budgets: dict[str, int]


def task_scope(state: ExecutionState) -> AuthorizationScope | None:
    """The authorization persisted on the task, or None if it is missing or tampered.

    Caller-supplied scopes are not a substitute: a wider budget or operation
    list on the call cannot expand the scope that was bound at task creation.
    """
    auth = state.authorization
    if auth is None:
        return None
    fingerprint = state.authorization_fingerprint
    if fingerprint is not None and fingerprint != auth.fingerprint():
        return None
    return auth


def effective_replan_limit(state: ExecutionState, enablement: ReplanEnablement) -> int:
    """The round cap for this task.

    The persisted task limit and the trusted configured maximum both apply.
    Untrusted state cannot raise the configured maximum, and a trusted context
    cannot raise a lower limit already stored on the task.
    """
    return min(int(state.replan_limit), int(enablement.max_rounds))


def plan_identity(steps: list[PlanStep]) -> str:
    """Stable identity of a plan that omits argument values and prompts."""
    payload = [
        {
            "step_id": step.step_id,
            "tool": step.resolved_tool_name(),
            "operation": _operation_name(step.arguments),
            "status": step.status.value,
            "collection": step.collection is not None,
        }
        for step in steps
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def call_is_mutating(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    registry: CapabilityRegistry | None = None,
) -> bool:
    """True when the capability registry marks this call as mutating."""
    reg = CAPABILITIES if registry is None else registry
    capability = reg.find(tool_name, operation_of(arguments or {}))
    return bool(capability and capability.mutating)


def historical_mutating_attempts(
    calls: list[ToolCallRecord],
    *,
    registry: CapabilityRegistry | None = None,
) -> int:
    """Durable mutation consumption: every recorded mutating attempt.

    Failures and retries count. The set of operations is the registry's
    ``mutating`` metadata, not a fixed name list.
    """
    return sum(
        1
        for call in calls
        if call_is_mutating(call.tool_name, call.arguments, registry=registry)
    )


def expansion_weight(step: PlanStep, state: ExecutionState) -> int:
    """How many concrete steps a plan step can become.

    A collection template is replaced by up to its selection size. When the
    source snapshot is already sealed, the weight is the actual selection.
    Otherwise the declared upper bound is used so a proposal cannot hide
    expansion behind a single template step.
    """
    work = step.collection
    if work is None:
        return 1
    snapshot = (state.collections.snapshots or {}).get(work.from_step)
    if snapshot is not None:
        selected = select_collection_items(snapshot, work.select, max_items=work.max_items)
        return len(selected)
    if work.select.mode == "first_n":
        requested = work.select.n if work.select.n is not None else work.max_items
        return min(work.max_items, requested)
    return min(work.max_items, len(work.select.ids or []))


def validate_revision(
    state: ExecutionState,
    scope: AuthorizationScope,
    replacement: list[PlanStep],
) -> RevisionVerdict:
    """Check the final effective plan against the original authorization.

    Effective steps are retained completed steps plus the replacement's
    concrete weight (collection templates count as their expansion, not as
    one step). Historical tool-call and mutation attempts stay consumed.
    """
    budgets = _budget_summary(state, scope, replacement, limit=state.replan_limit)
    completed = set(state.completed_steps)
    if not replacement:
        return RevisionVerdict(
            ok=False,
            reason="rejected:invalid_proposal",
            authorization="not_evaluated",
            budgets=budgets,
        )
    replacement_ids = [step.step_id for step in replacement]
    if len(replacement_ids) != len(set(replacement_ids)):
        return RevisionVerdict(
            ok=False,
            reason="rejected:invalid_proposal",
            authorization="not_evaluated",
            budgets=budgets,
        )
    if any(step_id in completed for step_id in replacement_ids):
        return RevisionVerdict(
            ok=False,
            reason="rejected:invalid_proposal",
            authorization="not_evaluated",
            budgets=budgets,
        )
    try:
        weights = [expansion_weight(step, state) for step in replacement]
    except CollectionError:
        return RevisionVerdict(
            ok=False,
            reason="rejected:invalid_proposal",
            authorization="not_evaluated",
            budgets=budgets,
        )
    retained = [step for step in state.plan if step.step_id in completed]
    effective_steps = len(retained) + sum(weights)
    mutations_proposed = sum(
        weight for step, weight in zip(replacement, weights, strict=True) if _step_is_mutating(step)
    )
    tool_calls_proposed = sum(weights)
    budgets = _budget_summary(
        state,
        scope,
        replacement,
        limit=state.replan_limit,
        effective_steps=effective_steps,
        mutations_proposed=mutations_proposed,
        tool_calls_proposed=tool_calls_proposed,
    )
    if effective_steps > scope.budgets.max_steps:
        return RevisionVerdict(
            ok=False,
            reason="budget_exceeded:max_steps",
            authorization="not_evaluated",
            budgets=budgets,
        )
    if budgets["mutations_consumed"] + mutations_proposed > scope.budgets.max_mutations:
        return RevisionVerdict(
            ok=False,
            reason="budget_exceeded:max_mutations",
            authorization="not_evaluated",
            budgets=budgets,
        )
    if budgets["tool_calls_consumed"] + tool_calls_proposed > scope.budgets.max_tool_calls:
        return RevisionVerdict(
            ok=False,
            reason="budget_exceeded:max_tool_calls",
            authorization="not_evaluated",
            budgets=budgets,
        )
    try:
        authorize_plan([*retained, *replacement], scope)
    except AuthorizationError as exc:
        if exc.reason.value == "BUDGET_EXCEEDED":
            budget = str((exc.details or {}).get("budget") or "budget")
            return RevisionVerdict(
                ok=False,
                reason=f"budget_exceeded:{budget}",
                authorization="denied",
                budgets=budgets,
            )
        return RevisionVerdict(
            ok=False,
            reason=f"rejected:{exc.reason.value}",
            authorization="denied",
            budgets=budgets,
        )
    return RevisionVerdict(
        ok=True,
        reason="accepted",
        authorization="authorized",
        budgets=budgets,
    )


class Replanner:
    """Propose remaining-work revisions under the original authorization scope."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        max_tokens: int = 4096,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_tokens = max_tokens

    def evaluate_trigger(
        self,
        *,
        category: str,
        state: ExecutionState,
        enablement: ReplanEnablement | None = None,
    ) -> ReplanDecision:
        if enablement is None or not enablement.enabled:
            return ReplanDecision(allowed=False, reason="replanning_disabled")
        if state.approval_status is ApprovalStatus.REJECTED or category in NON_REPLANABLE:
            refused = category if category in NON_REPLANABLE else "approval_rejection"
            return ReplanDecision(allowed=False, reason=f"non_replanable:{refused}")
        try:
            trigger = ReplanTrigger(category)
        except ValueError:
            return ReplanDecision(allowed=False, reason=f"unknown_trigger:{category}")
        limit = effective_replan_limit(state, enablement)
        if state.replan_count >= limit:
            return ReplanDecision(allowed=False, reason="replan_limit_exhausted")
        remaining = [
            step.step_id
            for step in state.plan
            if step.status.value in {"pending", "running"}
        ]
        completed = set(state.completed_steps)
        remaining = [sid for sid in remaining if sid not in completed]
        return ReplanDecision(
            allowed=True,
            trigger=trigger,
            reason="recoverable",
            remaining_step_ids=remaining,
        )

    async def propose_async(
        self,
        state: ExecutionState,
        scope: AuthorizationScope,
        *,
        trigger: ReplanTrigger,
        failure_summary: str,
        enablement: ReplanEnablement | None = None,
    ) -> ReplanResult:
        decision = self.evaluate_trigger(
            category=trigger.value, state=state, enablement=enablement
        )
        prior = [step.step_id for step in state.plan]
        if not decision.allowed:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count,
                trigger=trigger.value,
                reason=decision.reason,
                prior_plan_step_ids=prior,
            )
        # The argument named scope is intentionally unused: only the scope
        # persisted on the task can authorize a revision.
        del scope
        bound = task_scope(state)
        if bound is None:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count,
                trigger=trigger.value,
                reason="non_replanable:missing_authorization",
                prior_plan_step_ids=prior,
            )
        scope = bound

        completed = [
            {
                "step_id": step.step_id,
                "tool": step.resolved_tool_name(),
                "status": step.status.value,
            }
            for step in state.plan
            if step.step_id in set(state.completed_steps)
        ]
        remaining = [
            step.model_dump(mode="json")
            for step in state.plan
            if step.step_id in set(decision.remaining_step_ids)
        ]
        consumed_calls = len(state.tool_calls or [])
        payload = {
            "goal": state.user_goal,
            "trigger": trigger.value,
            "failure_summary": failure_summary[:500],
            "completed_steps": completed,
            "remaining_steps": remaining,
            "allowed_operations": list(scope.allowed_operations),
            "remaining_budgets": {
                "max_steps": scope.budgets.max_steps,
                "max_mutations": scope.budgets.max_mutations,
                "max_tool_calls": scope.budgets.max_tool_calls,
                "tool_calls_used": consumed_calls,
                "mutations_used": historical_mutating_attempts(list(state.tool_calls or [])),
            },
        }
        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "Propose a JSON plan for REMAINING work only. Do not repeat "
                        "completed steps. Do not expand permissions or budgets. "
                        "Return {\"steps\":[...]} using only allowed operations."
                    ),
                ),
                LLMMessage(role="user", content=str(payload)),
            ],
            temperature=0.0,
            max_tokens=self.max_tokens,
            metadata={"kind": "replan", "trigger": trigger.value},
        )
        try:
            draft = await complete_structured(self.llm, request, DynamicPlanDraft)
            plan = validate_plan(draft.to_plan(), self.registry)
        except (LLMProviderError, StructuredOutputError):
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason="rejected:provider",
                prior_plan_step_ids=prior,
            )
        except PlanValidationError:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason="rejected:validation",
                prior_plan_step_ids=prior,
            )

        proposed = list(plan.steps)
        verdict = validate_revision(state, scope, proposed)
        return ReplanResult(
            accepted=verdict.ok,
            revision=state.replan_count + 1,
            trigger=trigger.value,
            reason=verdict.reason,
            replacement_steps=proposed if verdict.ok else [],
            prior_plan_step_ids=prior,
            proposed_plan_id=plan_identity(proposed),
        )


def apply_replan(state: ExecutionState, result: ReplanResult) -> ExecutionState:
    """Replace unfinished steps with an accepted revision; keep completed history.

    The revision number on ``result`` cannot lower ``replan_count`` or skip
    ahead by more than one round. Replacement steps are stored as pending so a
    proposal cannot mark itself complete.
    """
    consumes = result.revision > state.replan_count
    next_count = state.replan_count + 1 if consumes else state.replan_count
    if not result.accepted or not consumes:
        if next_count == state.replan_count and result.trigger == (state.last_replan_trigger or ""):
            return state
        return state.model_copy(
            update={
                "replan_count": next_count,
                "last_replan_trigger": result.trigger or state.last_replan_trigger,
            }
        ).touch()

    completed_ids = set(state.completed_steps)
    kept = [step for step in state.plan if step.step_id in completed_ids]
    replacement = [
        step.model_copy(update={"status": PlanStepStatus.PENDING})
        for step in result.replacement_steps
    ]
    new_plan = [*kept, *replacement]
    current_step = next(
        (index for index, step in enumerate(new_plan) if step.step_id not in completed_ids),
        len(new_plan),
    )
    return state.model_copy(
        update={
            "plan": new_plan,
            "replan_count": next_count,
            "last_replan_trigger": result.trigger,
            "current_step": current_step,
            "final_status": state.final_status,
        }
    ).touch()


def commit_replan(
    store: SQLiteStore,
    state: ExecutionState,
    result: ReplanResult,
    *,
    scope: AuthorizationScope,
    enablement: ReplanEnablement | None,
) -> tuple[ReplanResult, ExecutionState]:
    """Persist a replan decision and its audit events in one transaction.

    An accepted result is validated again here. The accepted event is written
    in the same transaction as the revised state, so a revision cannot be
    reported as accepted unless that state commit succeeds. Disabled mode
    writes nothing.
    """
    if enablement is None or not enablement.enabled or result.reason == "replanning_disabled":
        refused = result.model_copy(
            update={
                "accepted": False,
                "reason": "replanning_disabled",
                "replacement_steps": [],
                "revision": state.replan_count,
            }
        )
        return refused, state

    state = _anchor_state(store, state)
    # Persisted task scope wins over the object the caller passed in.
    del scope
    bound = task_scope(state)
    trusted_limit = min(int(state.replan_limit), int(enablement.max_rounds))
    limit = effective_replan_limit(state, enablement)
    proposed_steps = list(result.replacement_steps)
    proposed_id = result.proposed_plan_id or (
        plan_identity(proposed_steps) if proposed_steps else None
    )
    verdict: RevisionVerdict | None = None
    if bound is not None and proposed_steps:
        verdict = validate_revision(state, bound, proposed_steps)
    budgets = (
        verdict.budgets
        if verdict is not None
        else _budget_summary(
            state,
            bound,
            proposed_steps,
            limit=trusted_limit,
        )
        if bound is not None
        else _empty_budgets(trusted_limit, state.replan_count)
    )
    budgets = {**budgets, "replan_limit": trusted_limit, "replan_count": state.replan_count}

    if result.reason == "replan_limit_exhausted" or state.replan_count >= limit:
        final = result.model_copy(
            update={
                "accepted": False,
                "reason": "replan_limit_exhausted",
                "replacement_steps": [],
                "revision": state.replan_count,
                "proposed_plan_id": proposed_id,
            }
        )
        updated = _clamp_limit(state, trusted_limit)
        _persist(
            store,
            state,
            updated,
            final,
            authorization="not_evaluated",
            budgets=budgets,
        )
        return final, updated

    if bound is None:
        final = result.model_copy(
            update={
                "accepted": False,
                "reason": "non_replanable:missing_authorization",
                "replacement_steps": [],
                "revision": state.replan_count,
                "proposed_plan_id": proposed_id,
            }
        )
        updated = _clamp_limit(state, trusted_limit)
        _persist(
            store,
            state,
            updated,
            final,
            authorization="denied",
            budgets=budgets,
        )
        return final, updated

    authorization: AuthorizationOutcome = "not_evaluated"
    if result.accepted:
        if verdict is not None and verdict.ok:
            authorization = verdict.authorization
            final = result.model_copy(
                update={
                    "accepted": True,
                    "revision": state.replan_count + 1,
                    "reason": "accepted",
                    "replacement_steps": proposed_steps,
                    "proposed_plan_id": proposed_id,
                }
            )
        else:
            authorization = verdict.authorization if verdict is not None else "not_evaluated"
            final = result.model_copy(
                update={
                    "accepted": False,
                    "revision": state.replan_count + 1,
                    "reason": verdict.reason if verdict is not None else "rejected:invalid_proposal",
                    "replacement_steps": [],
                    "proposed_plan_id": proposed_id,
                }
            )
    elif _consumes_round(result):
        authorization = (
            verdict.authorization
            if verdict is not None
            else "denied"
            if result.reason.startswith(("rejected:", "budget_exceeded:"))
            else "not_evaluated"
        )
        final = result.model_copy(
            update={
                "accepted": False,
                "revision": state.replan_count + 1,
                "replacement_steps": [],
                "proposed_plan_id": proposed_id,
            }
        )
    else:
        final = result.model_copy(
            update={
                "accepted": False,
                "revision": state.replan_count,
                "replacement_steps": [],
                "proposed_plan_id": proposed_id,
            }
        )

    updated = _clamp_limit(apply_replan(state, final), trusted_limit)
    budgets = {**budgets, "replan_limit": updated.replan_limit}
    _persist(
        store,
        state,
        updated,
        final,
        authorization=authorization,
        budgets=budgets,
    )
    return final, updated


async def propose_and_commit(
    store: SQLiteStore,
    replanner: Replanner,
    state: ExecutionState,
    scope: AuthorizationScope,
    *,
    trigger: ReplanTrigger,
    failure_summary: str,
    enablement: ReplanEnablement | None,
) -> tuple[ReplanResult, ExecutionState]:
    """Propose under the enablement gate and persist the decision atomically."""
    result = await replanner.propose_async(
        state,
        scope,
        trigger=trigger,
        failure_summary=failure_summary,
        enablement=enablement,
    )
    if enablement is None or not enablement.enabled or result.reason == "replanning_disabled":
        return result, state
    return commit_replan(store, state, result, scope=scope, enablement=enablement)


def _anchor_state(store: SQLiteStore, state: ExecutionState) -> ExecutionState:
    """Restore durable counters from the last committed snapshot.

    A caller can hand in a copy with a higher round cap, a lower round count,
    or tool-call history removed. None of those are allowed to loosen the
    budgets or the replan limit that were already persisted.
    """
    stored = store.get_state(state.task_id)
    if stored is None:
        return state
    merged_calls = list(stored.tool_calls or [])
    seen = {call.call_id for call in merged_calls}
    for call in state.tool_calls or []:
        if call.call_id not in seen:
            merged_calls.append(call)
    completed = list(
        dict.fromkeys([*(stored.completed_steps or []), *(state.completed_steps or [])])
    )
    stored_completed = {
        step.step_id: step
        for step in stored.plan
        if step.step_id in set(stored.completed_steps or [])
    }
    plan = list(state.plan)
    present = {step.step_id for step in plan}
    missing = [
        stored_completed[step_id]
        for step_id in stored.completed_steps or []
        if step_id in stored_completed and step_id not in present
    ]
    if missing:
        plan = [*missing, *plan]
    return state.model_copy(
        update={
            "plan": plan,
            "tool_calls": merged_calls,
            "completed_steps": completed,
            "replan_limit": min(int(stored.replan_limit), int(state.replan_limit)),
            "replan_count": max(int(stored.replan_count), int(state.replan_count)),
        }
    )


def _clamp_limit(state: ExecutionState, trusted_limit: int) -> ExecutionState:
    if state.replan_limit == trusted_limit:
        return state
    return state.model_copy(update={"replan_limit": trusted_limit}).touch()


def _consumes_round(result: ReplanResult) -> bool:
    if result.accepted:
        return True
    if result.reason in _NON_CONSUMING_REASONS:
        return False
    if result.reason.startswith(_NON_CONSUMING_PREFIXES):
        return False
    return True


def _persist(
    store: SQLiteStore,
    before: ExecutionState,
    updated: ExecutionState,
    result: ReplanResult,
    *,
    authorization: AuthorizationOutcome,
    budgets: dict[str, int],
) -> None:
    events = _replan_events(
        before,
        updated,
        result,
        budgets=budgets,
        authorization=authorization,
    )
    store.save_state_and_events(updated, events)


def _replan_events(
    before: ExecutionState,
    updated: ExecutionState,
    result: ReplanResult,
    *,
    budgets: dict[str, int],
    authorization: AuthorizationOutcome,
) -> list[Any]:
    proposed_steps = list(result.replacement_steps)
    proposed_id = result.proposed_plan_id or (
        plan_identity(proposed_steps) if proposed_steps else None
    )
    payload = {
        "task_id": before.task_id,
        "revision": result.revision,
        "trigger": _bounded(result.trigger),
        "previous_plan_id": plan_identity(before.plan),
        "proposed_plan_id": proposed_id,
        "accepted_plan_id": plan_identity(updated.plan) if result.accepted else None,
        "authorization": authorization,
        "budgets": {key: int(value) for key, value in budgets.items()},
        "outcome": _bounded(result.reason),
    }
    safe = {key: payload[key] for key in _EVENT_RESULT_KEYS}
    if result.reason == "replan_limit_exhausted":
        kinds = [(EventType.REPLAN_EXHAUSTED, "replan_exhausted")]
    elif result.accepted:
        kinds = [
            (EventType.REPLAN_PROPOSED, "replan_requested"),
            (EventType.REPLAN_ACCEPTED, "replan_accepted"),
        ]
    else:
        kinds = [
            (EventType.REPLAN_PROPOSED, "replan_requested"),
            (EventType.REPLAN_REJECTED, "replan_rejected"),
        ]
    return [
        make_event(
            task_id=before.task_id,
            event_type=event_type,
            action=action,
            result=dict(safe),
        )
        for event_type, action in kinds
    ]


def _empty_budgets(limit: int, replan_count: int) -> dict[str, int]:
    return {
        "max_steps": 0,
        "effective_steps": 0,
        "steps_remaining": 0,
        "max_mutations": 0,
        "mutations_consumed": 0,
        "mutations_proposed": 0,
        "mutations_remaining": 0,
        "max_tool_calls": 0,
        "tool_calls_consumed": 0,
        "tool_calls_proposed": 0,
        "tool_calls_remaining": 0,
        "replan_count": replan_count,
        "replan_limit": limit,
    }


def _budget_summary(
    state: ExecutionState,
    scope: AuthorizationScope,
    replacement: list[PlanStep],
    *,
    limit: int,
    effective_steps: int | None = None,
    mutations_proposed: int | None = None,
    tool_calls_proposed: int | None = None,
) -> dict[str, int]:
    consumed_calls = len(state.tool_calls or [])
    consumed_mutations = historical_mutating_attempts(list(state.tool_calls or []))
    if effective_steps is None:
        completed = {step.step_id for step in state.plan if step.step_id in set(state.completed_steps)}
        effective_steps = len(completed) + len(replacement)
    return {
        "max_steps": scope.budgets.max_steps,
        "effective_steps": effective_steps,
        "steps_remaining": max(0, scope.budgets.max_steps - effective_steps),
        "max_mutations": scope.budgets.max_mutations,
        "mutations_consumed": consumed_mutations,
        "mutations_proposed": 0 if mutations_proposed is None else mutations_proposed,
        "mutations_remaining": max(
            0,
            scope.budgets.max_mutations
            - consumed_mutations
            - (0 if mutations_proposed is None else mutations_proposed),
        ),
        "max_tool_calls": scope.budgets.max_tool_calls,
        "tool_calls_consumed": consumed_calls,
        "tool_calls_proposed": 0 if tool_calls_proposed is None else tool_calls_proposed,
        "tool_calls_remaining": max(
            0,
            scope.budgets.max_tool_calls
            - consumed_calls
            - (0 if tool_calls_proposed is None else tool_calls_proposed),
        ),
        "replan_count": state.replan_count,
        "replan_limit": limit,
    }


def _step_is_mutating(step: PlanStep) -> bool:
    tool = step.resolved_tool_name() or ""
    return call_is_mutating(tool, step.arguments)


def _operation_name(arguments: dict[str, Any] | None) -> str | None:
    operation = operation_of(arguments or {})
    return operation if isinstance(operation, str) else None


def _bounded(value: str, limit: int = 128) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]
