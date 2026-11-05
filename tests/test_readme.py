"""README commands must match the real CLI, grammar and exit codes."""

import re
import shlex
from pathlib import Path

import pytest

from app import cli
from app.cli import build_parser, main
from app.config import Settings
from app.llm.mock import parse_supported_goal

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
BASH_BLOCKS = re.findall(r"```bash\n(.*?)```", README, re.S)


def _cli_commands() -> list[list[str]]:
    """argv of every `aae ...` invocation documented in bash blocks."""
    commands = []
    for block in BASH_BLOCKS:
        for line in block.splitlines():
            tokens = shlex.split(line.strip()) if "aae" in line else []
            if "aae" in tokens:
                commands.append(tokens[tokens.index("aae") + 1 :])
    return commands


ALL_COMMANDS = _cli_commands()
COMMANDS = [argv for argv in ALL_COMMANDS if "--help" not in argv]


def test_readme_documents_help_and_every_cli_command() -> None:
    assert ["--help"] in ALL_COMMANDS
    used = {next(arg for arg in argv if not arg.startswith("-")) for argv in COMMANDS}
    assert {"run", "status", "approve", "reject", "resume", "history"} <= used


@pytest.mark.parametrize("argv", COMMANDS, ids=[" ".join(a)[:60] for a in COMMANDS])
def test_documented_command_parses(argv: list[str]) -> None:
    filled = [arg.replace("<task_id>", "task_" + "0" * 32) for arg in argv]
    build_parser().parse_args(filled)


def test_documented_goals_are_supported_by_the_mock() -> None:
    goals = [argv[argv.index("run") + 1] for argv in COMMANDS if "run" in argv]
    assert goals
    for goal in goals:
        assert parse_supported_goal(goal) is not None, goal


def test_documented_goals_use_single_quotes() -> None:
    for block in BASH_BLOCKS:
        for line in block.splitlines():
            if "$240" in line:
                assert "'" in line and '"' not in line, line


def test_exit_code_table_matches_cli() -> None:
    table = dict(re.findall(r"^\| (\d+) \| (.+?) \|$", README, re.M))
    assert {int(code) for code in table} == {
        cli.EXIT_OK,
        cli.EXIT_TASK_FAILED,
        cli.EXIT_USAGE,
        cli.EXIT_GOAL_REJECTED,
        cli.EXIT_NEEDS_APPROVAL,
        cli.EXIT_INVALID_TASK,
        cli.EXIT_LOCKED,
        cli.EXIT_INTERNAL,
    }


def test_readme_has_no_machine_specific_paths() -> None:
    assert "/home/" not in README


def test_documented_walkthrough_runs(tmp_path: Path, capsys) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
    )
    expected = {"Create": 0, "Update": 4}
    for argv in COMMANDS:
        if "run" not in argv:
            continue
        goal = argv[argv.index("run") + 1]
        assert main(argv, settings=settings, confirm=None) == expected[goal.split()[0]]
    capsys.readouterr()
