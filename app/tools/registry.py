"""Tool discovery and lookup."""

from __future__ import annotations

from app.tools.base import BaseTool


class ToolRegistry:
    """In-memory registry of reusable tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list_tools(self) -> list[BaseTool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def __contains__(self, name: str) -> bool:
        return name in self._tools
