"""Goal → InterpretedGoal → Plan pipeline (no execution)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.llm.base import LLMClient
from app.models.goal import InterpretedGoal
from app.models.plan import Plan
from app.planning.interpreter import GoalInterpreter
from app.planning.planner import Planner
from app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class PlanningResult:
    interpreted_goal: InterpretedGoal
    plan: Plan


class PlanningPipeline:
    """One-shot interpret + plan. Does not execute tools or mutate world state."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry) -> None:
        self.interpreter = GoalInterpreter(llm)
        self.planner = Planner(llm, registry)

    async def interpret_and_plan_async(self, goal_text: str) -> PlanningResult:
        interpreted = await self.interpreter.interpret_async(goal_text)
        plan = await self.planner.plan_async(interpreted)
        return PlanningResult(interpreted_goal=interpreted, plan=plan)

    def interpret_and_plan(self, goal_text: str) -> PlanningResult:
        return asyncio.run(self.interpret_and_plan_async(goal_text))

    def build_plan(self, goal_text: str) -> Plan:
        return self.interpret_and_plan(goal_text).plan
