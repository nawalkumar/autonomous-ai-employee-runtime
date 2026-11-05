"""Regression tests for the local CLI (in-process and real multi-process)."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import cli as cli_module
from app.cli import LOCK_BACKEND, TaskLockedError, build_runtime, main, task_lock
from app.config import Settings
from app.planning import PlanningPipeline
from app.llm import MockLLMClient
from app.runtime import EventType
from app.world import CompanyRepository

REPO_ROOT = Path(__file__).resolve().parents[1]
TICKET_GOAL = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
EMPLOYEE_GOAL = (
    "Update employee E-17 title to Senior Engineer and confirm the change in a note file."
)
SECRET = "TEST-ONLY-SENTINEL-NOT-A-CREDENTIAL"


# --- helpers ------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "root": tmp_path,
        "db": tmp_path / "data" / "runtime.db",
        "workspace": tmp_path / "workspace",
        "locks": tmp_path / "data" / "locks",
    }


@pytest.fixture
def settings(paths: dict[str, Path]) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(paths["db"]),
        workspace_path=str(paths["workspace"]),
    )


def call(settings: Settings, capsys, *argv: str, confirm=None) -> tuple[int, dict]:
    """In-process CLI call with --json; each call builds fresh runtime objects."""
    code = main(["--json", *argv], settings=settings, confirm=confirm)
    out = capsys.readouterr().out
    return code, json.loads(out)


def run_cli(paths: dict[str, Path], *argv: str, extra_env: dict | None = None,
            json_output: bool = True) -> subprocess.CompletedProcess:
    """Real subprocess: `python -m app.cli`."""
    return subprocess.run(
        _cli_command(*argv, json_output=json_output),
        cwd=paths["root"],
        env=_cli_env(paths, extra_env),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _cli_command(*argv: str, json_output: bool = True) -> list[str]:
    return [sys.executable, "-m", "app.cli", *(["--json"] if json_output else []), *argv]


def _cli_env(paths: dict[str, Path], extra_env: dict | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"XAI_API_KEY", "LLM_PROVIDER", "DATABASE_PATH", "WORKSPACE_PATH"}
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "DATABASE_PATH": str(paths["db"]),
            "WORKSPACE_PATH": str(paths["workspace"]),
        }
    )
    env.update(extra_env or {})
    return env


def as_json(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)


def task_rows(paths: dict[str, Path]) -> int:
    if not paths["db"].exists():
        return 0
    with sqlite3.connect(paths["db"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def events(settings: Settings, task_id: str) -> list:
    return build_runtime(settings).store.get_events(task_id)


def count(settings: Settings, task_id: str, event_type: str) -> int:
    return sum(1 for e in events(settings, task_id) if e.event_type == event_type)


def paused_employee_task(settings: Settings, capsys) -> str:
    code, payload = call(settings, capsys, "run", EMPLOYEE_GOAL)
    assert code == 4
    return payload["task"]["task_id"]


# --- 1, 8: run ----------------------------------------------------------------


def test_run_canonical_ticket_completes_and_verifies(settings: Settings, capsys) -> None:
    code, payload = call(settings, capsys, "run", TICKET_GOAL)

    task = payload["task"]
    assert code == 0
    assert payload["provider"] == "mock"
    assert task["status"] == "completed"
    assert task["verification_status"] == "passed"
    assert task["steps_completed"] == task["steps_total"] == 2
    assert task["verification_checks_passed"] == task["verification_checks_total"] > 0
    assert len(build_runtime(settings).repository.list_tickets()) == 1


def test_run_with_injected_503_retries_once_and_verifies(settings: Settings, capsys) -> None:
    code, payload = call(
        settings, capsys, "run", TICKET_GOAL, "--inject-transient", "create_ticket"
    )

    assert code == 0
    assert payload["task"]["status"] == "completed"
    assert payload["task"]["verification_status"] == "passed"
    assert payload["task"]["retry_count"] == 1
    assert len(build_runtime(settings).repository.list_tickets()) == 1


def test_human_output_shows_provider_paths_and_formats(
    settings: Settings, paths: dict[str, Path], capsys
) -> None:
    code = main(["run", TICKET_GOAL], settings=settings, confirm=None)
    out = capsys.readouterr().out

    assert code == 0
    assert "Provider:  mock" in out
    assert "Create a support ticket for Acme Corp" in out
    assert str(paths["db"].resolve()) in out
    assert str(paths["workspace"].resolve()) in out
    assert "Status:        COMPLETED" in out
    assert "Verification:  PASSED" in out


# --- 2, 4, 5: approval --------------------------------------------------------


def test_employee_update_pauses_then_approval_completes(settings: Settings, capsys) -> None:
    code, payload = call(settings, capsys, "run", EMPLOYEE_GOAL)
    task = payload["task"]
    assert code == 4
    assert task["status"] == "needs_approval"
    assert task["pending_action"] == {
        "step_id": "step_1",
        "tool": "company_api",
        "arguments": {
            "operation": "update_employee",
            "employee_id": "E-17",
            "fields": {"title": "Senior Engineer"},
        },
        "risk": "write",
        "reason": "Employee record mutation requires human approval.",
    }

    code, payload = call(
        settings, capsys, "approve", task["task_id"], "--step", "step_1", "--yes"
    )
    assert code == 0
    assert payload["task"]["status"] == "completed"
    assert payload["task"]["verification_status"] == "passed"
    repo = build_runtime(settings).repository
    assert repo.get_employee("E-17").title == "Senior Engineer"


def _assert_still_pending(settings: Settings, task_id: str, events_before: int) -> None:
    state = build_runtime(settings).runtime.load(task_id)
    assert state.final_status.value == "needs_approval"
    assert state.approval_status.value == "pending"
    assert len(events(settings, task_id)) == events_before
    assert build_runtime(settings).repository.get_employee("E-17").title == "Software Engineer"


def test_wrong_step_refuses_approval(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)
    before = len(events(settings, task_id))

    code, payload = call(settings, capsys, "approve", task_id, "--step", "step_2", "--yes")

    assert code == 5
    assert "pending step is 'step_1'" in payload["error"]
    _assert_still_pending(settings, task_id, before)


def test_non_interactive_approval_without_yes_is_refused(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)
    before = len(events(settings, task_id))

    code, payload = call(settings, capsys, "approve", task_id, confirm=None)

    assert code == 5
    assert "--yes" in payload["error"]
    _assert_still_pending(settings, task_id, before)


def test_interactive_confirmation_is_required(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)
    before = len(events(settings, task_id))
    prompts: list[str] = []

    def decline(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    code, _ = call(settings, capsys, "approve", task_id, confirm=decline)
    assert code == 5
    assert "company_api update_employee for step step_1" in prompts[0]
    _assert_still_pending(settings, task_id, before)

    code, payload = call(settings, capsys, "approve", task_id, confirm=lambda _: True)
    assert code == 0
    assert payload["task"]["status"] == "completed"


def test_approving_twice_is_refused(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)
    assert call(settings, capsys, "approve", task_id, "--yes")[0] == 0

    code, payload = call(settings, capsys, "approve", task_id, "--yes")
    assert code == 5
    assert "no pending approval" in payload["error"]
    assert count(settings, task_id, EventType.APPROVAL_APPROVED) == 1


# --- 6: reject ----------------------------------------------------------------


def test_reject_aborts_with_reason_and_no_mutation(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)

    code, payload = call(settings, capsys, "reject", task_id, "--reason", "Budget freeze")

    assert code == 1
    assert payload["task"]["status"] == "aborted"
    assert payload["task"]["approval_status"] == "rejected"
    assert payload["task"]["completion_summary"] == "Budget freeze"
    cli_rt = build_runtime(settings)
    assert cli_rt.repository.get_employee("E-17").title == "Software Engineer"
    assert os.listdir(cli_rt.workspace_path) == []

    assert call(settings, capsys, "approve", task_id, "--yes")[0] == 5
    code, payload = call(settings, capsys, "resume", task_id)
    assert code == 1
    assert payload["task"]["status"] == "aborted"


# --- 7, 9: rejected goals and unknown tasks -----------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "Refund $240 to Acme Corp.",
        "Do not create a support ticket for Acme Corp about the $240 billing discrepancy.",
        "",
    ],
)
def test_unsupported_goal_creates_nothing(
    settings: Settings, paths: dict[str, Path], capsys, goal: str
) -> None:
    code, payload = call(settings, capsys, "run", goal)

    assert code == 3
    assert "no task was created" in payload["error"]
    assert task_rows(paths) == 0
    cli_rt = build_runtime(settings)
    assert cli_rt.repository.list_tickets() == []
    assert os.listdir(cli_rt.workspace_path) == []


@pytest.mark.parametrize("command", ["status", "approve", "reject", "resume", "history"])
def test_unknown_task_exits_5(settings: Settings, capsys, command: str) -> None:
    code, payload = call(settings, capsys, command, "task_" + "0" * 32)
    assert code == 5
    assert "Unknown task" in payload["error"]


@pytest.mark.parametrize("task_id", ["../escape", "a/b", "..\\x", "x" * 200, ""])
def test_invalid_task_ids_are_rejected_before_locking(
    settings: Settings, paths: dict[str, Path], capsys, task_id: str
) -> None:
    code, payload = call(settings, capsys, "approve", task_id, "--yes")
    assert code == 5
    assert "Invalid task id" in payload["error"]
    assert not list(paths["root"].glob("**/*escape*"))


# --- 10, 11, 17: locking ------------------------------------------------------


def test_lock_backend_matches_platform() -> None:
    assert LOCK_BACKEND == ("msvcrt" if os.name == "nt" else "fcntl")


def test_lock_is_exclusive_per_task_and_released_after_exception(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    with task_lock(lock_dir, "task_a"):
        with pytest.raises(TaskLockedError):
            with task_lock(lock_dir, "task_a"):
                pass
        with task_lock(lock_dir, "task_b"):
            pass

    with pytest.raises(RuntimeError):
        with task_lock(lock_dir, "task_a"):
            raise RuntimeError("boom")
    with task_lock(lock_dir, "task_a"):
        pass
    # Stale lock files are harmless: only OS locks matter.
    assert (lock_dir / "lock-task_a.lock").exists()


def test_cross_process_lock_contention_exits_6_without_changes(
    settings: Settings, paths: dict[str, Path], capsys
) -> None:
    task_id = paused_employee_task(settings, capsys)
    before = len(events(settings, task_id))

    with task_lock(paths["locks"], task_id):
        approve = run_cli(paths, "approve", task_id, "--yes")
        resume = run_cli(paths, "resume", task_id)
        reject = run_cli(paths, "reject", task_id)

    for proc in (approve, resume, reject):
        assert proc.returncode == 6, proc.stdout + proc.stderr
        assert "locked by another CLI process" in as_json(proc)["error"]
    _assert_still_pending(settings, task_id, before)

    assert run_cli(paths, "approve", task_id, "--yes").returncode == 0


def test_lock_released_when_holder_process_is_killed(
    settings: Settings, paths: dict[str, Path], capsys
) -> None:
    task_id = paused_employee_task(settings, capsys)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; from app.cli import task_lock\n"
            "ctx = task_lock(Path(sys.argv[1]), sys.argv[2]); ctx.__enter__()\n"
            "print('locked', flush=True); time.sleep(120)",
            str(paths["locks"]),
            task_id,
        ],
        cwd=paths["root"],
        env=_cli_env(paths),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert run_cli(paths, "approve", task_id, "--yes").returncode == 6
    finally:
        holder.kill()
        holder.wait(timeout=30)

    # Windows may release a killed process's lock asynchronously; allow a bounded
    # window in which the only acceptable outcome other than success is "locked".
    deadline = time.monotonic() + 15
    proc = run_cli(paths, "approve", task_id, "--yes")
    while proc.returncode == 6 and time.monotonic() < deadline:
        time.sleep(0.25)
        proc = run_cli(paths, "approve", task_id, "--yes")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert as_json(proc)["task"]["status"] == "completed"


# --- 12: concurrency ----------------------------------------------------------


def _parallel(paths: dict[str, Path], n: int, *argv: str) -> list[subprocess.CompletedProcess]:
    procs = [
        subprocess.Popen(
            _cli_command(*argv),
            cwd=paths["root"],
            env=_cli_env(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(n)
    ]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        results.append(subprocess.CompletedProcess(proc.args, proc.returncode, out, err))
    return results


def test_concurrent_approvals_approve_exactly_once(
    settings: Settings, paths: dict[str, Path], capsys
) -> None:
    task_id = paused_employee_task(settings, capsys)

    results = _parallel(paths, 4, "approve", task_id, "--yes")

    codes = sorted(r.returncode for r in results)
    assert codes.count(0) == 1, [r.stdout + r.stderr for r in results]
    assert set(codes) <= {0, 5, 6}
    assert count(settings, task_id, EventType.APPROVAL_APPROVED) == 1
    assert count(settings, task_id, EventType.TASK_COMPLETED) == 1
    state = build_runtime(settings).runtime.load(task_id)
    assert [c.step_id for c in state.tool_calls] == ["step_1", "step_2"]


def test_concurrent_resumes_execute_exactly_once(
    settings: Settings, paths: dict[str, Path]
) -> None:
    cli_rt = build_runtime(settings)
    planned = PlanningPipeline(MockLLMClient(), cli_rt.registry).interpret_and_plan(TICKET_GOAL)
    task = cli_rt.runtime.create_task(
        user_goal=TICKET_GOAL, interpreted_goal=planned.interpreted_goal, plan=planned.plan.steps
    )

    results = _parallel(paths, 4, "resume", task.task_id)

    assert set(r.returncode for r in results) <= {0, 6}, [r.stderr for r in results]
    assert any(r.returncode == 0 for r in results)
    assert count(settings, task.task_id, EventType.TOOL_CALLED) == 2
    assert count(settings, task.task_id, EventType.TASK_COMPLETED) == 1
    assert len(CompanyRepository(paths["db"]).list_tickets()) == 1


# --- 13, 14, 15: configuration and secrets -------------------------------------


def test_secrets_never_appear_in_output(paths: dict[str, Path]) -> None:
    env = {"XAI_API_KEY": SECRET, "LLM_PROVIDER": "xai"}
    run = run_cli(paths, "run", TICKET_GOAL, extra_env=env, json_output=False)
    task_id = json.loads(
        run_cli(paths, "run", TICKET_GOAL, extra_env=env).stdout
    )["task"]["task_id"]
    outputs = [
        run,
        run_cli(paths, "status", task_id, extra_env=env, json_output=False),
        run_cli(paths, "history", task_id, extra_env=env),
        run_cli(paths, "--provider", "xai", "run", TICKET_GOAL, extra_env=env),
        run_cli(paths, "run", "Refund $240 to Acme Corp.", extra_env=env),
        run_cli(paths, "status", "missing", extra_env=env),
    ]
    for proc in outputs:
        assert SECRET not in proc.stdout
        assert SECRET not in proc.stderr


def test_output_redacts_secret_values() -> None:
    out = cli_module.Output(as_json=False, secrets=[SECRET])
    assert out.redact(f"failed calling api with {SECRET}") == "failed calling api with ***"


def test_database_inside_workspace_is_rejected(paths: dict[str, Path]) -> None:
    proc = run_cli(
        paths,
        "run",
        TICKET_GOAL,
        extra_env={"DATABASE_PATH": str(paths["workspace"] / "runtime.db")},
    )
    assert proc.returncode == 2
    assert "outside WORKSPACE_PATH" in as_json(proc)["error"]
    assert not (paths["workspace"] / "runtime.db").exists()


def test_lock_directory_inside_workspace_is_rejected(tmp_path: Path, capsys) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "runtime.db"),
        workspace_path=str(tmp_path / "locks"),
    )
    code, payload = call(settings, capsys, "status", "task_x")
    assert code == 2
    assert "Lock directory" in payload["error"]


def test_mock_is_default_even_when_environment_requests_xai(paths: dict[str, Path]) -> None:
    proc = run_cli(paths, "run", TICKET_GOAL, extra_env={"LLM_PROVIDER": "xai"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = as_json(proc)
    assert payload["provider"] == "mock"
    assert payload["task"]["status"] == "completed"


def test_xai_provider_is_explicitly_disabled(paths: dict[str, Path]) -> None:
    proc = run_cli(paths, "--provider", "xai", "run", TICKET_GOAL)
    assert proc.returncode == 2
    assert "disabled" in as_json(proc)["error"]
    assert task_rows(paths) == 0


def test_usage_errors_exit_2(settings: Settings, capsys) -> None:
    assert main(["bogus"], settings=settings) == 2
    assert main(["approve"], settings=settings) == 2
    capsys.readouterr()


# --- 16: history --------------------------------------------------------------


def test_history_shows_persisted_events_in_order(settings: Settings, capsys) -> None:
    task_id = paused_employee_task(settings, capsys)
    call(settings, capsys, "approve", task_id, "--yes")

    code, payload = call(settings, capsys, "history", task_id)

    stored = events(settings, task_id)
    assert code == 0
    assert [e["event_id"] for e in payload["events"]] == [e.event_id for e in stored]
    types = [e["event_type"] for e in payload["events"]]
    assert types[0] == EventType.TASK_CREATED
    assert types.index(EventType.APPROVAL_REQUESTED) < types.index(EventType.APPROVAL_APPROVED)
    assert types[-1] == EventType.TASK_COMPLETED

    main(["history", task_id], settings=settings)
    human = capsys.readouterr().out
    assert f"History for {task_id} ({len(stored)} events)" in human
    assert "approval.approved" in human


# --- 3, 18: real multi-process end-to-end -------------------------------------


def test_multi_process_approval_workflow_survives_restarts(paths: dict[str, Path]) -> None:
    run = run_cli(paths, "run", EMPLOYEE_GOAL)
    assert run.returncode == 4, run.stdout + run.stderr
    task_id = as_json(run)["task"]["task_id"]

    status = run_cli(paths, "status", task_id)
    assert status.returncode == 0
    assert as_json(status)["task"]["pending_action"]["step_id"] == "step_1"

    refused = run_cli(paths, "approve", task_id)
    assert refused.returncode == 5

    approve = run_cli(paths, "approve", task_id, "--step", "step_1", "--yes")
    assert approve.returncode == 0, approve.stdout + approve.stderr
    assert as_json(approve)["task"]["verification_status"] == "passed"

    history = run_cli(paths, "history", task_id)
    types = [e["event_type"] for e in as_json(history)["events"]]
    assert types.count(EventType.APPROVAL_APPROVED) == 1
    assert types.count(EventType.TASK_COMPLETED) == 1

    human = run_cli(paths, "status", task_id, json_output=False)
    assert "Status:        COMPLETED" in human.stdout
    assert CompanyRepository(paths["db"]).get_employee("E-17").title == "Senior Engineer"
    assert (paths["workspace"] / "employee_update.txt").exists()


# --- file-permission errors -----------------------------------------------------

posix_non_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits are not enforced on Windows or for root",
)


@pytest.fixture
def restore_modes():
    changed: list[tuple[Path, int]] = []

    def chmod(path: Path, mode: int) -> None:
        changed.append((path, path.stat().st_mode))
        path.chmod(mode)

    yield chmod
    for path, mode in reversed(changed):
        path.chmod(mode)


def _assert_clean_permission_error(capsys, code: int, fragment: str) -> None:
    captured = capsys.readouterr()
    assert code == 2
    assert fragment in captured.err
    assert "Traceback" not in captured.err + captured.out


@posix_non_root
def test_unwritable_database_file_exits_2(settings: Settings, paths, capsys, restore_modes) -> None:
    task_id = paused_employee_task(settings, capsys)
    restore_modes(paths["db"], 0o444)

    for argv in (["status", task_id], ["approve", task_id, "--yes"], ["run", TICKET_GOAL]):
        code = main(argv, settings=settings, confirm=None)
        _assert_clean_permission_error(capsys, code, "Database file is not readable and writable")


@posix_non_root
def test_unwritable_data_directory_exits_2(settings: Settings, paths, capsys, restore_modes) -> None:
    build_runtime(settings)
    paths["db"].unlink()
    restore_modes(paths["db"].parent, 0o555)

    code = main(["run", TICKET_GOAL], settings=settings, confirm=None)
    _assert_clean_permission_error(capsys, code, "Data directory is not writable")


@posix_non_root
def test_unwritable_workspace_exits_2(settings: Settings, paths, capsys, restore_modes) -> None:
    build_runtime(settings)
    restore_modes(paths["workspace"], 0o555)

    code = main(["run", TICKET_GOAL], settings=settings, confirm=None)
    _assert_clean_permission_error(capsys, code, "Workspace directory is not writable")
    assert task_rows(paths) == 0


@posix_non_root
def test_unwritable_lock_directory_exits_2(settings: Settings, paths, capsys, restore_modes) -> None:
    task_id = paused_employee_task(settings, capsys)
    store = cli_module.SQLiteStore(paths["db"])
    before = len(store.get_events(task_id))
    restore_modes(paths["locks"], 0o555)

    code = main(["approve", task_id, "--yes"], settings=settings, confirm=None)
    _assert_clean_permission_error(capsys, code, "Lock directory is not writable")
    assert store.get_state(task_id).final_status.value == "needs_approval"
    assert len(store.get_events(task_id)) == before


@posix_non_root
def test_unopenable_lock_file_exits_2(settings: Settings, paths, capsys, restore_modes) -> None:
    task_id = paused_employee_task(settings, capsys)
    before = len(events(settings, task_id))
    lock_file = paths["locks"] / f"lock-{task_id}.lock"
    lock_file.touch()
    restore_modes(lock_file, 0o000)

    code = main(["approve", task_id, "--yes"], settings=settings, confirm=None)
    _assert_clean_permission_error(capsys, code, "Permission denied opening lock file")
    _assert_still_pending(settings, task_id, before)


def test_permission_errors_do_not_leak_secrets(paths, capsys, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(paths["db"]),
        workspace_path=str(paths["workspace"]),
        xai_api_key=SECRET,
    )

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", f"/secret/{SECRET}")

    monkeypatch.setattr(cli_module, "cmd_status", denied)
    code = main(["status", "task_x"], settings=settings, confirm=None)
    captured = capsys.readouterr()
    assert code == 2
    assert SECRET not in captured.out + captured.err
    assert "Permission denied" in captured.err


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("attempt to write a readonly database", 2),
        ("unable to open database file", 2),
        ("no such table: tasks", 70),
    ],
)
def test_sqlite_access_errors_map_to_2_and_others_stay_internal(
    settings: Settings, capsys, monkeypatch, message: str, expected: int
) -> None:
    def failing(*_args, **_kwargs):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(cli_module, "cmd_status", failing)
    code = main(["status", "task_x"], settings=settings, confirm=None)
    captured = capsys.readouterr()
    assert code == expected
    assert "Traceback" not in captured.err


def test_programming_errors_are_not_reported_as_configuration_errors(
    settings: Settings, capsys, monkeypatch
) -> None:
    def broken(*_args, **_kwargs):
        raise AttributeError("bug")

    monkeypatch.setattr(cli_module, "cmd_status", broken)
    code = main(["status", "task_x"], settings=settings, confirm=None)
    assert code == 70
    assert "Unexpected error (AttributeError)" in capsys.readouterr().err
