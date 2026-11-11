"""HTTP API (local mode) through real ASGI requests against real SQLite/workspace."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.redaction import REDACTED, Redactor
from app.cli import build_runtime, main, task_lock
from app.config import Settings
from app.llm.mock import parse_supported_goal
from app.main import create_app
from app.planning import PlanningPipeline
from app.llm import MockLLMClient
from app.runtime.locking import hold_task_lock
from app.runtime.transitions import make_event
from tests.api_support import (
    EMPLOYEE_GOAL,
    EMPLOYEE_NOTE_GOAL,
    TASK_ID,
    TICKET_GOAL,
    TICKET_NOTE_GOAL,
    employee_title,
    event_types,
    query,
    submit,
    wait_for,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
        allowed_hosts="testserver",
    )


@pytest.fixture
def db(settings: Settings) -> Path:
    return Path(settings.database_path)


@pytest.fixture
def workspace(settings: Settings) -> Path:
    return Path(settings.workspace_path)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def paused_employee_task(client: TestClient, goal: str = EMPLOYEE_GOAL) -> dict:
    created = submit(client, goal)
    return wait_for(client, created["task_id"], {"needs_approval"})


# --- Meta ---------------------------------------------------------------------------


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "local"
    assert body["persistence"] == "persistent"
    assert re.fullmatch(r"[0-9a-f]{16}", body["boot_id"])


def test_scenarios_only_offer_goals_the_mock_planner_accepts(client: TestClient) -> None:
    body = client.get("/api/v1/scenarios").json()
    assert {s["scenario_id"] for s in body["scenarios"]} == {"ticket", "employee"}
    for scenario in body["scenarios"]:
        for goal in scenario["goals"]:
            assert parse_supported_goal(goal) is not None, goal
    assert body["failure_injection_enabled"] is True


# --- 1-4: ticket goals --------------------------------------------------------------


def test_ticket_goal_creates_a_real_persisted_task_and_ticket(
    client: TestClient, db: Path
) -> None:
    response = client.post("/api/v1/tasks", json={"goal": TICKET_GOAL})
    assert response.status_code == 202
    created = response.json()
    assert re.fullmatch(TASK_ID, created["task_id"])
    assert created["status"] == "queued"
    assert response.headers["location"] == f"/api/v1/tasks/{created['task_id']}"

    done = wait_for(client, created["task_id"], {"completed", "failed"})
    assert done["status"] == "completed"
    assert [s["operation"] for s in done["plan"]] == ["find_customer", "create_ticket"]

    rows = query(db, "SELECT task_id, status FROM tasks")
    assert rows == [(created["task_id"], "completed")]
    tickets = query(db, "SELECT ticket_id, customer_id, subject FROM company_tickets")
    assert tickets == [("T-10001", "C-1001", "Billing discrepancy $240")]
    api_tickets = client.get("/api/v1/company/tickets").json()["tickets"]
    assert [t["ticket_id"] for t in api_tickets] == ["T-10001"]


def test_ticket_with_note_writes_the_note_to_the_workspace(
    client: TestClient, workspace: Path
) -> None:
    created = submit(client, TICKET_NOTE_GOAL)
    done = wait_for(client, created["task_id"], {"completed", "failed"})
    assert done["status"] == "completed"

    note = workspace / "acme_billing_summary.txt"
    assert note.read_text(encoding="utf-8").startswith("Summary: Support ticket created")
    files = client.get("/api/v1/workspace/files").json()["files"]
    assert files == [{"path": "acme_billing_summary.txt", "size_bytes": note.stat().st_size}]
    content = client.get("/api/v1/workspace/file", params={"path": "acme_billing_summary.txt"})
    assert content.json()["content"] == note.read_text(encoding="utf-8")


# --- 5-10: approval -----------------------------------------------------------------


def test_employee_goal_requires_approval_and_approve_executes_once(
    client: TestClient, db: Path, workspace: Path
) -> None:
    paused = paused_employee_task(client, EMPLOYEE_NOTE_GOAL)
    task_id = paused["task_id"]
    pending = paused["pending_action"]
    assert pending == {
        "step_id": "step_1",
        "tool": "company_api",
        "operation": "update_employee",
        "arguments": {
            "operation": "update_employee",
            "employee_id": "E-17",
            "fields": {"title": "Senior Engineer"},
        },
        "risk": "write",
        "reason": "Employee record mutation requires human approval.",
        "created_at": pending["created_at"],
    }
    assert paused["tool_calls"] == []
    assert employee_title(db) == "Software Engineer"

    response = client.post(
        f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert employee_title(db) == "Senior Engineer"
    assert (workspace / "employee_update.txt").exists()

    history = client.get(f"/api/v1/tasks/{task_id}/history").json()["events"]
    approved = [e for e in history if e["event_type"] == "approval.approved"]
    assert len(approved) == 1
    assert approved[0]["actor"] == "human"
    assert approved[0]["result"]["approver"] == "local-dashboard"
    calls = [e for e in history if e["event_type"] == "TOOL_CALLED"]
    assert [c["arguments"]["operation"] for c in calls] == ["update_employee", "write"]


def test_reject_aborts_without_executing_the_write(client: TestClient, db: Path) -> None:
    task_id = paused_employee_task(client)["task_id"]
    response = client.post(
        f"/api/v1/tasks/{task_id}/reject",
        json={"step_id": "step_1", "reason": "Not this quarter", "confirm": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "aborted"
    assert body["approval_status"] == "rejected"
    assert body["completion_summary"] == "Not this quarter"
    assert body["tool_calls"] == []
    assert employee_title(db) == "Software Engineer"
    assert query(db, "SELECT COUNT(*) FROM company_idempotency") == [(0,)]
    types = event_types(client, task_id)
    assert "approval.rejected" in types and "TOOL_CALLED" not in types

    again = client.post(
        f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "no_pending_approval"
    assert employee_title(db) == "Software Engineer"


@pytest.mark.parametrize(
    "body",
    [
        {"step_id": "step_1"},
        {"step_id": "step_1", "confirm": False},
        {"step_id": "../x", "confirm": True},
        {"step_id": "step_1", "confirm": True, "extra": 1},
    ],
)
def test_approval_requires_explicit_confirmation_and_valid_fields(
    client: TestClient, db: Path, body: dict
) -> None:
    task_id = paused_employee_task(client)["task_id"]
    response = client.post(f"/api/v1/tasks/{task_id}/approve", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"
    assert employee_title(db) == "Software Engineer"


def test_approval_for_a_different_step_is_refused(client: TestClient, db: Path) -> None:
    task_id = paused_employee_task(client)["task_id"]
    response = client.post(
        f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_2", "confirm": True}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "step_mismatch"
    assert employee_title(db) == "Software Engineer"


def test_concurrent_approvals_execute_the_write_once(client: TestClient, db: Path) -> None:
    task_id = paused_employee_task(client)["task_id"]
    barrier = threading.Barrier(6)
    results: list[int] = []

    def approve() -> None:
        barrier.wait()
        response = client.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=approve) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(results) == [200] + [409] * 5
    types = event_types(client, task_id)
    assert types.count("approval.approved") == 1
    assert types.count("TOOL_CALLED") == 1
    assert query(db, "SELECT COUNT(*) FROM company_idempotency") == [(1,)]


# --- 11-12: injected failure ----------------------------------------------------------


def test_injected_503_is_retried_within_budget_and_recorded(
    client: TestClient, db: Path
) -> None:
    created = submit(client, TICKET_NOTE_GOAL, inject_failure="create_ticket")
    done = wait_for(client, created["task_id"], {"completed", "failed"})
    assert done["status"] == "completed"
    assert done["retry_count"] == 1
    attempts = {s["step_id"]: s["attempts"] for s in done["plan"]}
    assert attempts == {"step_1": 1, "step_2": 2, "step_3": 1}
    failed = [c for c in done["tool_calls"] if c["status"] == "failed"]
    assert len(failed) == 1
    assert "simulated" in failed[0]["error_message"]

    history = client.get(f"/api/v1/tasks/{created['task_id']}/history").json()["events"]
    types = [e["event_type"] for e in history]
    injected = history[types.index("demo.failure_injected")]
    assert injected["result"]["simulated"] is True
    for expected in (
        "TOOL_FAILED",
        "failure.detected",
        "recovery.evaluated",
        "recovery.retry_started",
        "recovery.retry_succeeded",
        "verification.passed",
        "TASK_COMPLETED",
    ):
        assert types.count(expected) == 1, expected
    evaluated = history[types.index("recovery.evaluated")]["result"]
    assert evaluated["action"] == "retry"
    assert evaluated["max_recovery_attempts"] == 2
    assert query(db, "SELECT COUNT(*) FROM company_tickets") == [(1,)]


def test_injection_is_refused_when_the_plan_has_no_matching_step(
    client: TestClient, db: Path
) -> None:
    response = client.post(
        "/api/v1/tasks", json={"goal": EMPLOYEE_GOAL, "inject_failure": "create_ticket"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "injection_not_applicable"
    assert query(db, "SELECT COUNT(*) FROM tasks") == [(0,)]


def test_injection_can_be_disabled(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "data" / "runtime.db"),
        workspace_path=str(tmp_path / "workspace"),
        allowed_hosts="testserver",
        allow_failure_injection=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks", json={"goal": TICKET_GOAL, "inject_failure": "create_ticket"}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "injection_disabled"


# --- 13-15: persistence and verification ---------------------------------------------


def test_history_survives_an_application_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        task_id = paused_employee_task(first)["task_id"]
        before = first.get(f"/api/v1/tasks/{task_id}/history").json()

    with TestClient(create_app(settings)) as second:
        assert second.get(f"/api/v1/tasks/{task_id}/history").json() == before
        assert second.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"
        response = second.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        assert response.json()["status"] == "completed"


def test_verification_reports_independent_checks_against_world_state(
    client: TestClient, db: Path
) -> None:
    created = submit(client, TICKET_NOTE_GOAL)
    wait_for(client, created["task_id"], {"completed", "failed"})
    verification = client.get(f"/api/v1/tasks/{created['task_id']}/verification").json()
    assert verification["status"] == "passed"
    assert verification["checks_total"] == verification["checks_passed"] == 6
    ticket_check = next(c for c in verification["checks"] if c["name"] == "ticket_exists")
    stored = query(db, "SELECT ticket_id, subject FROM company_tickets")[0]
    assert ticket_check["actual"]["ticket_id"] == stored[0]
    assert ticket_check["actual"]["subject"] == stored[1]
    types = {e["type"] for e in verification["evidence"]}
    assert {"ticket_record", "file_record", "file_content"} <= types


def test_verification_failure_is_reported_with_reasons(
    client: TestClient, settings: Settings
) -> None:
    rt = build_runtime(settings)
    planned = PlanningPipeline(MockLLMClient(), rt.registry).interpret_and_plan(TICKET_NOTE_GOAL)
    state = rt.runtime.create_task(
        user_goal=TICKET_NOTE_GOAL, interpreted_goal=planned.interpreted_goal,
        plan=planned.plan.steps,
    )
    # Execute the steps, then remove the note before verification runs.
    for _ in planned.plan.steps[:-1]:
        rt.runtime.execute_next(state.task_id)
    original = rt.registry.get("file").execute

    def write_then_delete(args, *, idempotency_key=None):
        result = original(args, idempotency_key=idempotency_key)
        (Path(settings.workspace_path) / "acme_billing_summary.txt").unlink()
        return result

    rt.registry.get("file").execute = write_then_delete
    rt.runtime.run(state.task_id)

    detail = client.get(f"/api/v1/tasks/{state.task_id}").json()
    assert detail["status"] == "failed"
    verification = client.get(f"/api/v1/tasks/{state.task_id}/verification").json()
    assert verification["status"] == "failed"
    assert verification["checks_passed"] < verification["checks_total"]
    assert any("acme_billing_summary.txt" in r for r in verification["failure_reasons"])


# --- 16: unsupported goals ------------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "Delete all employees",
        "Update employee E-17 title to CEO",
        "Do not update employee E-17 title to Senior Engineer",
        TICKET_GOAL + " Also update employee E-17 title to Senior Engineer.",
    ],
)
def test_unsupported_goal_is_rejected_with_zero_writes(
    client: TestClient, db: Path, workspace: Path, goal: str
) -> None:
    response = client.post("/api/v1/tasks", json={"goal": goal})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "goal_rejected"
    assert query(db, "SELECT COUNT(*) FROM tasks") == [(0,)]
    assert query(db, "SELECT COUNT(*) FROM events") == [(0,)]
    assert query(db, "SELECT COUNT(*) FROM company_tickets") == [(0,)]
    assert employee_title(db) == "Software Engineer"
    assert list(workspace.iterdir()) == []
    assert client.get("/api/v1/tasks").json()["total"] == 0


@pytest.mark.parametrize(
    "body",
    [{}, {"goal": ""}, {"goal": "x" * 301}, {"goal": TICKET_GOAL, "tool": "file"},
     {"goal": TICKET_GOAL, "inject_failure": "update_employee"}],
)
def test_task_request_validation(client: TestClient, db: Path, body: dict) -> None:
    response = client.post("/api/v1/tasks", json=body)
    assert response.status_code == 422
    assert not db.exists()


# --- Duplicate submissions ------------------------------------------------------------


def test_idempotency_key_replays_the_same_task(client: TestClient, db: Path) -> None:
    headers = {"Idempotency-Key": "submit-0001"}
    first = client.post("/api/v1/tasks", json={"goal": TICKET_GOAL}, headers=headers)
    second = client.post("/api/v1/tasks", json={"goal": TICKET_GOAL}, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["task_id"] == second.json()["task_id"]
    assert second.json()["idempotent_replay"] is True

    conflict = client.post("/api/v1/tasks", json={"goal": EMPLOYEE_GOAL}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    wait_for(client, first.json()["task_id"], {"completed"})
    assert query(db, "SELECT COUNT(*) FROM tasks") == [(1,)]
    assert query(db, "SELECT COUNT(*) FROM company_tickets") == [(1,)]


def test_concurrent_duplicate_submissions_create_one_task(client: TestClient, db: Path) -> None:
    barrier = threading.Barrier(5)
    ids: list[str] = []

    def post() -> None:
        barrier.wait()
        response = client.post(
            "/api/v1/tasks", json={"goal": TICKET_GOAL}, headers={"Idempotency-Key": "dup-key-42"}
        )
        assert response.status_code in (200, 202), response.text
        ids.append(response.json()["task_id"])

    threads = [threading.Thread(target=post) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(ids) == 5 and len(set(ids)) == 1
    wait_for(client, ids[0], {"completed"})
    assert query(db, "SELECT COUNT(*) FROM tasks") == [(1,)]
    assert query(db, "SELECT COUNT(*) FROM company_tickets") == [(1,)]


def test_invalid_idempotency_key_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks", json={"goal": TICKET_GOAL}, headers={"Idempotency-Key": "../bad"}
    )
    assert response.status_code == 422


# --- Listing, lookup, resume ------------------------------------------------------------


def test_task_list_and_lookup(client: TestClient, settings: Settings, capsys) -> None:
    created = [submit(client, TICKET_GOAL)["task_id"] for _ in range(3)]
    for task_id in created:
        wait_for(client, task_id, {"completed"})
    assert main(["--json", "run", EMPLOYEE_GOAL], settings=settings) == 4
    capsys.readouterr()

    listing = client.get("/api/v1/tasks", params={"limit": 2}).json()
    assert listing["total"] == 4 and len(listing["tasks"]) == 2
    everything = client.get("/api/v1/tasks", params={"limit": 10}).json()["tasks"]
    assert {t["status"] for t in everything} == {"completed", "needs_approval"}
    assert set(created) <= {t["task_id"] for t in everything}

    assert client.get("/api/v1/tasks/task_unknown").status_code == 404
    assert client.get("/api/v1/tasks/bad%2Fid").status_code in (404, 422)
    assert client.get("/api/v1/tasks/" + "x" * 129).status_code == 422


def test_resume_runs_an_interrupted_task_and_refuses_terminal_ones(
    client: TestClient, settings: Settings
) -> None:
    rt = build_runtime(settings)
    planned = rt.planning().interpret_and_plan(TICKET_GOAL)
    state = rt.runtime.create_task(
        user_goal=TICKET_GOAL, interpreted_goal=planned.interpreted_goal, plan=planned.plan.steps
    )
    detail = client.get(f"/api/v1/tasks/{state.task_id}").json()
    assert detail["status"] == "queued" and detail["resumable"] is True

    response = client.post(f"/api/v1/tasks/{state.task_id}/resume", json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    again = client.post(f"/api/v1/tasks/{state.task_id}/resume", json={"confirm": True})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "task_terminal"

    paused = paused_employee_task(client)["task_id"]
    waiting = client.post(f"/api/v1/tasks/{paused}/resume", json={"confirm": True})
    assert waiting.json()["error"]["code"] == "awaiting_approval"


# --- Shared CLI/API locking --------------------------------------------------------------


def test_api_refuses_while_the_cli_lock_is_held(client: TestClient, db: Path, settings) -> None:
    task_id = paused_employee_task(client)["task_id"]
    lock_dir = Path(settings.database_path).parent / "locks"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; from app.cli import task_lock\n"
            "ctx = task_lock(Path(sys.argv[1]), sys.argv[2]); ctx.__enter__()\n"
            "print('locked', flush=True); time.sleep(120)",
            str(lock_dir),
            task_id,
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        for action, body in (
            ("approve", {"step_id": "step_1", "confirm": True}),
            ("reject", {"step_id": "step_1", "confirm": True}),
        ):
            response = client.post(f"/api/v1/tasks/{task_id}/{action}", json=body)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "task_locked"
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert employee_title(db) == "Software Engineer"
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"


def test_cli_refuses_while_the_api_holds_the_lock(
    client: TestClient, settings: Settings, capsys
) -> None:
    task_id = paused_employee_task(client)["task_id"]
    lock_dir = Path(settings.database_path).parent / "locks"
    with hold_task_lock(lock_dir, task_id):
        code = main(["--json", "approve", task_id, "--yes"], settings=settings)
        assert code == 6
        assert "locked" in capsys.readouterr().out
    with task_lock(lock_dir, task_id):
        pass


def test_cli_and_api_see_the_same_task(client: TestClient, settings: Settings, capsys) -> None:
    task_id = paused_employee_task(client)["task_id"]
    assert main(["--json", "approve", task_id, "--yes"], settings=settings) == 0
    capsys.readouterr()
    detail = client.get(f"/api/v1/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    approved = [
        e
        for e in client.get(f"/api/v1/tasks/{task_id}/history").json()["events"]
        if e["event_type"] == "approval.approved"
    ]
    assert approved[0]["result"]["approver"].startswith("cli")


# --- 19: workspace paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path,status",
    [
        ("../data/runtime.db", 400),
        ("..", 400),
        ("/etc/passwd", 400),
        ("C:/Windows/win.ini", 400),
        ("..\\data\\runtime.db", 400),
        ("a/../../data/runtime.db", 400),
        ("%2e%2e/data/runtime.db", 400),
        ("runtime.db", 404),
        ("missing.txt", 404),
    ],
)
def test_workspace_path_traversal_is_denied(
    client: TestClient, path: str, status: int
) -> None:
    response = client.get("/api/v1/workspace/file", params={"path": path})
    assert response.status_code == status
    assert "SQLite" not in response.text


def test_database_files_in_the_workspace_are_never_listed_or_read(
    client: TestClient, workspace: Path, db: Path
) -> None:
    assert client.get("/api/v1/tasks").status_code == 200
    (workspace / "copy.db").write_bytes(db.read_bytes())
    (workspace / "disguised.txt").write_bytes(db.read_bytes())
    (workspace / "ok.txt").write_text("hello", encoding="utf-8")
    files = [f["path"] for f in client.get("/api/v1/workspace/files").json()["files"]]
    assert files == ["ok.txt"]
    for name in ("copy.db", "disguised.txt"):
        assert client.get("/api/v1/workspace/file", params={"path": name}).status_code == 404


def test_symlink_escaping_the_workspace_is_not_served(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        (workspace / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not permitted on this platform")
    response = client.get("/api/v1/workspace/file", params={"path": "link.txt"})
    assert response.status_code in (403, 404)
    assert "outside" not in response.text


# --- Request hygiene ---------------------------------------------------------------------


def test_request_guards(client: TestClient, db: Path) -> None:
    oversized = client.post(
        "/api/v1/tasks",
        content=b'{"goal": "' + b"x" * 20_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    form = client.post("/api/v1/tasks", data={"goal": TICKET_GOAL})
    assert form.status_code == 415
    cross = client.post(
        "/api/v1/tasks", json={"goal": TICKET_GOAL}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    same = client.post(
        "/api/v1/tasks", json={"goal": TICKET_GOAL}, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 202
    rebinding = client.get("/api/v1/tasks", headers={"Host": "attacker.example"})
    assert rebinding.status_code == 400
    assert rebinding.json()["error"]["code"] == "invalid_host"
    assert client.get("/api/v1/tools").status_code == 404
    assert client.post("/api/v1/tools/file", json={}).status_code == 404


def test_security_headers(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


def test_unexpected_errors_do_not_leak_details(settings: Settings, monkeypatch) -> None:
    app = create_app(settings)

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail /home/someone/data/runtime.db")

    monkeypatch.setattr(app.state.aae.service, "list_tasks", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/tasks")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret" not in response.text and "Traceback" not in response.text
    assert "/home/" not in response.text


def test_sensitive_values_are_redacted_from_history(
    client: TestClient, settings: Settings
) -> None:
    task_id = paused_employee_task(client)["task_id"]
    build_runtime(settings).store.append_event(
        make_event(
            task_id=task_id,
            event_type="test.sensitive",
            arguments={"api_key": "sk-live-123", "nested": {"password": "hunter2"}, "ok": 1},
        )
    )
    events = client.get(f"/api/v1/tasks/{task_id}/history").json()["events"]
    sensitive = next(e for e in events if e["event_type"] == "test.sensitive")
    assert sensitive["arguments"] == {
        "api_key": REDACTED,
        "nested": {"password": REDACTED},
        "ok": 1,
    }


def test_redactor_masks_configured_secrets() -> None:
    redact = Redactor(["TOPSECRETVALUE"])
    assert redact({"note": "x TOPSECRETVALUE y", "token": "a"}) == {
        "note": f"x {REDACTED} y",
        "token": REDACTED,
    }
