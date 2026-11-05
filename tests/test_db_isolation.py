"""Regression tests: the runtime database is unreachable through the FileTool."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, database_inside_workspace
from app.models import ExecutionState
from app.models.enums import FailureType
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import FileTool, ToolRegistry
from app.verify import OutcomeVerifier
from app.world import CompanyRepository


def test_default_database_is_outside_workspace() -> None:
    settings = Settings(_env_file=None)
    assert not database_inside_workspace(settings.database_path, settings.workspace_path)


def test_settings_reject_database_inside_workspace() -> None:
    with pytest.raises(ValidationError, match="outside WORKSPACE_PATH"):
        Settings(
            _env_file=None,
            database_path="workspace/runtime.db",
            workspace_path="workspace",
        )


def test_runtime_rejects_database_inside_workspace(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="outside the workspace"):
        ExecutionRuntime(
            store=SQLiteStore(ws / "runtime.db"),
            registry=registry,
            workspace_path=ws,
        )


def test_file_tool_blocks_database_read_and_write(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SQLiteStore(ws / "legacy.db")  # simulates an old in-workspace DB
    tool = FileTool(ws)

    cases = [
        {"operation": "read", "path": "legacy.db"},
        {"operation": "write", "path": "legacy.db", "content": "overwritten"},
        {"operation": "write", "path": "new.sqlite", "content": "x"},
        {"operation": "write", "path": "nested/other.sqlite3", "content": "x"},
        {"operation": "write", "path": "legacy.db-wal", "content": "x"},
        {"operation": "write", "path": "LEGACY.DB", "content": "x"},
    ]
    for args in cases:
        result = tool.invoke(args)
        assert result.ok is False, args
        assert result.error_type == FailureType.POLICY_BLOCKED.value, args

    # Database still intact and usable.
    store.save_state(ExecutionState(task_id="t1", user_goal="g"))
    assert store.get_state("t1") is not None

    listing = tool.invoke({"operation": "list", "path": ""})
    assert listing.ok is True
    assert "legacy.db" not in listing.data["files"]


def test_file_tool_blocks_renamed_sqlite_file_by_header(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    SQLiteStore(ws / "disguised.txt")
    tool = FileTool(ws)

    for args in (
        {"operation": "read", "path": "disguised.txt"},
        {"operation": "write", "path": "disguised.txt", "content": "x"},
    ):
        result = tool.invoke(args)
        assert result.ok is False, args
        assert result.error_type == FailureType.POLICY_BLOCKED.value


def _database_outside_workspace(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    SQLiteStore(data / "runtime.db")
    ws = tmp_path / "workspace"
    ws.mkdir()
    return data, ws


def _assert_all_blocked(tool: FileTool, cases: list[dict]) -> None:
    for args in cases:
        result = tool.invoke(args)
        assert result.ok is False, args
        assert result.error_type == FailureType.POLICY_BLOCKED.value, args


def _symlink_or_skip(target: Path, link: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable on this system: {exc}")


def test_file_tool_blocks_traversal_to_database(tmp_path: Path) -> None:
    data, ws = _database_outside_workspace(tmp_path)
    _assert_all_blocked(
        FileTool(ws),
        [
            {"operation": "read", "path": "../data/runtime.db"},
            {"operation": "write", "path": "../data/runtime.db", "content": "x"},
            {"operation": "read", "path": str((data / "runtime.db").resolve())},
        ],
    )


def test_file_tool_blocks_symlinks_to_database(tmp_path: Path) -> None:
    data, ws = _database_outside_workspace(tmp_path)
    _symlink_or_skip(data / "runtime.db", ws / "link.txt")
    _symlink_or_skip(data, ws / "datadir", is_dir=True)
    _assert_all_blocked(
        FileTool(ws),
        [
            {"operation": "read", "path": "link.txt"},
            {"operation": "write", "path": "link.txt", "content": "x"},
            {"operation": "write", "path": "datadir/runtime.db", "content": "x"},
        ],
    )


def test_file_tool_protected_paths(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = FileTool(ws, protected_paths=[ws / "private"])
    blocked = tool.invoke(
        {"operation": "write", "path": "private/a.txt", "content": "x"}
    )
    assert blocked.ok is False
    assert blocked.error_type == FailureType.POLICY_BLOCKED.value


def test_legitimate_file_operations_still_work(tmp_path: Path) -> None:
    tool = FileTool(tmp_path / "ws")
    for path in ("notes/summary.txt", "database_notes.txt", "report.md"):
        write = tool.invoke({"operation": "write", "path": path, "content": "ok"})
        assert write.ok is True, path
        read = tool.invoke({"operation": "read", "path": path})
        assert read.ok is True and read.data["content"] == "ok"


def test_verifier_rejects_database_file_criterion(tmp_path: Path) -> None:
    from app.models import SuccessCriterion

    ws = tmp_path / "ws"
    ws.mkdir()
    SQLiteStore(ws / "legacy.db")
    verifier = OutcomeVerifier(CompanyRepository(tmp_path / "world.db"), ws)
    state = ExecutionState(
        user_goal="g",
        success_criteria=[
            SuccessCriterion(
                description="db",
                criterion_type="file_exists",
                expected_value={"path": "legacy.db"},
            )
        ],
    )
    result = verifier.verify(state)
    assert result.passed is False
