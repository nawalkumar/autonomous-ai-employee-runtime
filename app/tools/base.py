"""Reusable tool abstraction."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.models.enums import FailureType
from app.tools.result import ToolResult


class RiskLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class BaseTool(ABC):
    """Provider-agnostic tool interface used by a future orchestrator."""

    name: str
    description: str
    risk_level: RiskLevel
    args_schema: type[BaseModel]

    def invoke(
        self,
        args: BaseModel | dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        """Validate arguments and execute the tool."""
        try:
            parsed = (
                args
                if isinstance(args, self.args_schema)
                else self.args_schema.model_validate(args)
            )
        except ValidationError as exc:
            return ToolResult.failure(
                FailureType.INVALID_ARGS.value,
                f"Invalid arguments for tool '{self.name}': {exc.errors()}",
            )
        return self.execute(parsed, idempotency_key=idempotency_key)

    @abstractmethod
    def execute(
        self,
        args: BaseModel,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        """Perform the tool side effect and return a normalized result."""

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "args_schema": self.args_schema.model_json_schema(),
        }
