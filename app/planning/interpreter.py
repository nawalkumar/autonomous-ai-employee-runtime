"""Natural-language goal → InterpretedGoal."""

from __future__ import annotations

import asyncio

from app.llm.base import LLMClient, LLMMessage, LLMRequest
from app.llm.structured import StructuredOutputError, complete_structured
from app.models.goal import InterpretedGoal
from app.planning.prompts import GOAL_INTERPRETER_SYSTEM_PROMPT
from app.planning.schemas import InterpretedGoalDraft


class InterpretationError(ValueError):
    """Raised when goal interpretation fails."""


class GoalInterpreter:
    """LLM-backed goal interpreter (understand only — never executes)."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def interpret_async(self, natural_language_goal: str) -> InterpretedGoal:
        goal_text = natural_language_goal.strip()
        if not goal_text:
            raise InterpretationError("Goal text must not be empty")

        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=GOAL_INTERPRETER_SYSTEM_PROMPT),
                LLMMessage(role="user", content=goal_text),
            ],
            temperature=0.0,
            metadata={"kind": "interpret"},
        )
        try:
            draft = await complete_structured(
                self.llm, request, InterpretedGoalDraft
            )
        except StructuredOutputError as exc:
            raise InterpretationError(str(exc)) from exc
        except Exception as exc:  # provider/network/mock routing errors
            raise InterpretationError(f"Goal interpretation failed: {exc}") from exc

        return draft.to_interpreted_goal(goal_text)

    def interpret(self, natural_language_goal: str) -> InterpretedGoal:
        return asyncio.run(self.interpret_async(natural_language_goal))
