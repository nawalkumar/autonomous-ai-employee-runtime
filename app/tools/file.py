"""Sandboxed filesystem tool restricted to the workspace directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import FailureType
from app.tools.base import BaseTool, RiskLevel
from app.tools.result import ToolResult

FileOperation = Literal["read", "write", "list"]


class FileToolArgs(BaseModel):
    operation: FileOperation
    path: str = Field(default="", description="Path relative to the workspace root")
    content: str | None = None

    @model_validator(mode="after")
    def validate_operation_args(self) -> "FileToolArgs":
        if self.operation == "write" and self.content is None:
            raise ValueError("content is required for write")
        if self.operation in {"read", "write"} and not self.path:
            raise ValueError("path is required")
        return self


class FileTool(BaseTool):
    name = "file"
    description = "Read and write files inside the sandboxed workspace directory."
    args_schema = FileToolArgs
    risk_level = RiskLevel.WRITE

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def risk_for(self, operation: FileOperation) -> RiskLevel:
        return RiskLevel.WRITE if operation == "write" else RiskLevel.READ

    def metadata(self) -> dict[str, Any]:
        meta = super().metadata()
        meta["workspace_root"] = str(self.workspace_root)
        meta["operations"] = {
            "read": RiskLevel.READ.value,
            "write": RiskLevel.WRITE.value,
            "list": RiskLevel.READ.value,
        }
        return meta

    def execute(
        self,
        args: BaseModel,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        assert isinstance(args, FileToolArgs)
        _ = idempotency_key

        try:
            target = self._resolve_safe_path(args.path)
        except ValueError as exc:
            return ToolResult.failure(
                FailureType.POLICY_BLOCKED.value,
                str(exc),
            )

        if args.operation == "write":
            return self._write(target, args.content or "")
        if args.operation == "read":
            return self._read(target)
        return self._list(target if args.path else self.workspace_root)

    def _resolve_safe_path(self, relative_path: str) -> Path:
        if relative_path is None:
            raise ValueError("path is required")

        # Reject absolute paths and explicit parent traversal tokens early.
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError(
                f"Absolute paths are not allowed outside workspace: {relative_path}"
            )
        if ".." in candidate.parts:
            raise ValueError(f"Path traversal is not allowed: {relative_path}")

        resolved = (self.workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Resolved path escapes workspace: {relative_path}"
            ) from exc
        return resolved

    def _write(self, target: Path, content: str) -> ToolResult:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = str(target.relative_to(self.workspace_root))
        return ToolResult.success(
            {"path": rel, "bytes_written": len(content.encode("utf-8"))},
            evidence=[{"type": "file_written", "data": {"path": rel}}],
        )

    def _read(self, target: Path) -> ToolResult:
        if not target.exists() or not target.is_file():
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"File not found: {target.relative_to(self.workspace_root)}",
            )
        content = target.read_text(encoding="utf-8")
        rel = str(target.relative_to(self.workspace_root))
        return ToolResult.success(
            {"path": rel, "content": content},
            evidence=[{"type": "file_read", "data": {"path": rel}}],
        )

    def _list(self, target: Path) -> ToolResult:
        if target.is_file():
            target = target.parent
        if not target.exists():
            return ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Directory not found under workspace: {target}",
            )
        entries = sorted(
            str(p.relative_to(self.workspace_root))
            for p in target.rglob("*")
            if p.is_file()
        )
        return ToolResult.success({"files": entries, "count": len(entries)})
