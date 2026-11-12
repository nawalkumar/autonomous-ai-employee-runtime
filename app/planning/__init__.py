"""Goal interpretation and planning (LLM understands/plans; runtime executes).

Submodules may be imported directly (``app.planning.intents``) without pulling
the full planning stack, which avoids circular imports with ``app.authz``.
"""

from app.planning.interpreter import GoalInterpreter, InterpretationError
from app.planning.planner import Planner, PlanningError
from app.planning.validator import PlanValidationError, validate_plan

__all__ = [
    "DynamicPlanner",
    "GoalInterpreter",
    "InterpretationError",
    "PlanValidationError",
    "Planner",
    "PlanningError",
    "PlanningPipeline",
    "PlanningResult",
    "SUPPORTED_DYNAMIC_INTENTS",
    "TrustedIntent",
    "parse_trusted_intent",
    "validate_plan",
]


def __getattr__(name: str):
    if name in {"PlanningPipeline", "PlanningResult"}:
        from app.planning.pipeline import PlanningPipeline, PlanningResult

        return {"PlanningPipeline": PlanningPipeline, "PlanningResult": PlanningResult}[name]
    if name == "DynamicPlanner":
        from app.planning.dynamic import DynamicPlanner

        return DynamicPlanner
    if name in {"SUPPORTED_DYNAMIC_INTENTS", "TrustedIntent", "parse_trusted_intent"}:
        from app.planning import intents as _intents

        return getattr(_intents, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
