"""The demo page must stay a labelled simulation whose values match real CLI runs."""

import json
import re
from pathlib import Path

import pytest

from app.cli import build_runtime, main
from app.config import Settings
from app.llm.mock import parse_supported_goal

DEMO = (Path(__file__).resolve().parents[1] / "demo-ui" / "index.html").read_text(
    encoding="utf-8"
)


def _goal(name: str) -> str:
    match = re.search(rf'{name}: "([^"]+)"', DEMO)
    assert match, name
    return match.group(1)


def test_demo_is_labelled_as_a_scripted_simulation() -> None:
    assert "Scripted simulation" in DEMO
    assert "not connected to the runtime" in DEMO
    assert "Simulation Demo" in DEMO


def test_goal_field_is_read_only_and_goals_are_canonical() -> None:
    assert re.search(r'<textarea id="goalInput"[^>]*\breadonly\b', DEMO)
    for name in ("recovery", "approval"):
        assert parse_supported_goal(_goal(name)) is not None


@pytest.mark.parametrize(
    "stale",
    ["TKT-", "human_approval", "workspace/support", "acme-billing", "get_employee",
     "High-risk", "Math.random", "18, 1, 2"],
)
def test_stale_or_fabricated_values_are_gone(stale: str) -> None:
    assert stale not in DEMO


def test_injected_503_is_labelled_as_simulated() -> None:
    assert "503 Service Unavailable (simulated by --inject-transient)" in DEMO


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
    )


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_recovery_values_match_a_real_cli_run(settings: Settings, capsys) -> None:
    code = main(
        ["--json", "run", _goal("recovery"), "--inject-transient", "create_ticket"],
        settings=settings,
    )
    task = _json(capsys)["task"]
    assert code == 0

    cli_rt = build_runtime(settings)
    ticket = cli_rt.repository.list_tickets()[0]
    files = sorted(p.name for p in cli_rt.workspace_path.iterdir())
    ops = [s["operation"] for s in task["steps"]]

    assert ticket.ticket_id in DEMO
    assert ticket.subject in DEMO
    assert all(name in DEMO for name in files)
    assert "find_customer → create_ticket → file.write" in DEMO
    assert ops == ["find_customer", "create_ticket", "write"]
    checks = f"{task['verification_checks_passed']}/{task['verification_checks_total']} checks"
    assert f"PASSED · {checks} · {task['evidence_count']} evidence items" in DEMO
    assert task["retry_count"] == 1


def test_approval_values_match_a_real_cli_run(settings: Settings, capsys) -> None:
    assert main(["--json", "run", _goal("approval")], settings=settings) == 4
    paused = _json(capsys)["task"]
    pending = paused["pending_action"]

    assert json.dumps(pending["arguments"], sort_keys=True) in DEMO
    assert f'"risk": "{pending["risk"]}"' not in DEMO  # risk shown as a plain value
    assert f">{pending['risk']}<" in DEMO
    assert pending["reason"] in DEMO
    assert pending["step_id"] in DEMO
    assert "update_employee → file.write" in DEMO

    assert main(
        ["--json", "approve", paused["task_id"], "--step", pending["step_id"], "--yes"],
        settings=settings,
    ) == 0
    task = _json(capsys)["task"]
    checks = f"{task['verification_checks_passed']}/{task['verification_checks_total']} checks"
    assert f"PASSED · {checks} · {task['evidence_count']} evidence items" in DEMO
    assert all(
        name in DEMO for name in (p.name for p in build_runtime(settings).workspace_path.iterdir())
    )

    history = build_runtime(settings).store.get_events(task["task_id"])
    for event_type in ("approval.requested", "approval.approved"):
        assert event_type in {e.event_type for e in history}
        assert event_type in DEMO
    assert "approval.rejected" in DEMO
