"""Goal → InterpretedGoal → Plan pipeline (no execution)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.authz.errors import AuthorizationError
from app.authz.intent_scopes import authorization_for_goal, authorization_for_trusted_intent
from app.authz.models import AuthorizationScope
from app.authz.origin import authorization_for_supported_goal
from app.authz.plan_check import authorize_plan
from app.config import Settings, get_settings
from app.llm.base import LLMClient
from app.llm.mock import parse_supported_goal
from app.models.goal import InterpretedGoal
from app.models.plan import Plan
from app.planning.collection_plans import plan_for_collection_intent
from app.planning.dynamic import DynamicPlanner
from app.planning.intents import COLLECTION_INTENT_IDS, parse_trusted_intent
from app.planning.interpreter import GoalInterpreter, InterpretationError
from app.planning.planner import Planner, PlanningError
from app.planning.validator import PlanValidationError, validate_plan
from app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class PlanningResult:
    interpreted_goal: InterpretedGoal
    plan: Plan
    authorization: AuthorizationScope | None = None


class PlanningPipeline:
    """One-shot interpret + plan. Does not execute tools or mutate world state."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.interpreter = GoalInterpreter(llm)
        self.planner = Planner(llm, registry)
        cfg = settings or Settings()
        self.dynamic_planner = DynamicPlanner(
            llm,
            registry,
            max_provider_retries=cfg.llm_max_provider_retries,
            max_tokens=cfg.llm_max_tokens,
        )

    @property
    def dynamic_enabled(self) -> bool:
        cfg = self.settings or get_settings()
        return bool(cfg.aae_dynamic_planning_enabled)

    async def interpret_and_plan_async(self, goal_text: str) -> PlanningResult:
        if self.dynamic_enabled:
            return await self._dynamic_interpret_and_plan(goal_text)
        interpreted = await self.interpreter.interpret_async(goal_text)
        plan = await self.planner.plan_async(interpreted)
        return PlanningResult(interpreted_goal=interpreted, plan=plan)

    def interpret_and_plan(self, goal_text: str) -> PlanningResult:
        return asyncio.run(self.interpret_and_plan_async(goal_text))

    def build_plan(self, goal_text: str) -> Plan:
        return self.interpret_and_plan(goal_text).plan

    async def _dynamic_interpret_and_plan(self, goal_text: str) -> PlanningResult:
        cfg = self.settings or get_settings()
        max_recovery = cfg.max_recovery_attempts

        # Preserve the original two mock contracts exactly (including scope).
        if parse_supported_goal(goal_text) is not None:
            interpreted = await self.interpreter.interpret_async(goal_text)
            plan = await self.planner.plan_async(interpreted)
            authorization = authorization_for_supported_goal(
                goal_text, max_recovery_attempts=max_recovery
            )
            return PlanningResult(
                interpreted_goal=interpreted,
                plan=plan,
                authorization=authorization,
            )

        intent = parse_trusted_intent(goal_text)
        if intent is None:
            raise InterpretationError(
                "Goal is not a supported trusted intent; dynamic planning fails closed."
            )

        authorization = authorization_for_trusted_intent(
            intent, max_recovery_attempts=max_recovery
        )
        interpreted = intent.to_interpreted_goal()
        # Collection grammars are planned here. The provider must not choose
        # the source path, the item field, the bound, or extra mutations.
        if intent.intent_id in COLLECTION_INTENT_IDS:
            plan = plan_for_collection_intent(intent)
            try:
                plan = validate_plan(plan, self.registry)
                authorize_plan(plan, authorization)
            except PlanValidationError as exc:
                raise PlanningError(f"Collection plan invalid: {exc}") from exc
            except AuthorizationError as exc:
                raise PlanningError(f"Collection plan unauthorized: {exc}") from exc
            return PlanningResult(
                interpreted_goal=interpreted,
                plan=plan,
                authorization=authorization,
            )
        try:
            plan = await self.dynamic_planner.plan_async(
                interpreted,
                authorization,
                intent_id=intent.intent_id,
            )
        except PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlanningError(f"Dynamic planning failed: {exc}") from exc

        return PlanningResult(
            interpreted_goal=interpreted,
            plan=plan,
            authorization=authorization,
        )

    def resolve_authorization(self, goal_text: str) -> AuthorizationScope:
        """Trusted scope derivation used by CLI/API after planning."""
        cfg = self.settings or get_settings()
        return authorization_for_goal(
            goal_text,
            dynamic_planning_enabled=self.dynamic_enabled,
            max_recovery_attempts=cfg.max_recovery_attempts,
        )
