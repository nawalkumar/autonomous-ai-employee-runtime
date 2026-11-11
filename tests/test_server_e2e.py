"""End-to-end over real HTTP against a separate uvicorn process and real storage."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.api_support import (
    EMPLOYEE_NOTE_GOAL,
    TICKET_NOTE_GOAL,
    employee_title,
    query,
)
from tests.live_server import LiveServer, base_env


def wait_status(client: httpx.Client, task_id: str, statuses: set[str]) -> dict:
    deadline = time.monotonic() + 30
    while True:
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        if detail["status"] in statuses and not detail["executing"]:
            return detail
        assert time.monotonic() < deadline, detail["status"]
        time.sleep(0.05)


@pytest.fixture
def server(tmp_path: Path):
    live = LiveServer(tmp_path).start()
    yield live
    live.stop()


def test_approval_concurrency_restart_and_cli_interop(server: LiveServer, tmp_path: Path) -> None:
    db = tmp_path / "data" / "runtime.db"
    with httpx.Client(base_url=server.url, timeout=30) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"goal": EMPLOYEE_NOTE_GOAL},
            headers={"Idempotency-Key": "e2e-approval-1"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        paused = wait_status(client, task_id, {"needs_approval"})
        assert paused["pending_action"]["operation"] == "update_employee"
        assert employee_title(db) == "Software Engineer"

        unauthorized = client.post(
            f"/api/v1/tasks/{task_id}/approve",
            json={"step_id": "step_1", "confirm": True},
            headers={"Origin": "https://attacker.example"},
        )
        assert unauthorized.status_code == 403
        assert employee_title(db) == "Software Engineer"

    barrier = threading.Barrier(8)
    codes: list[int] = []

    def approve() -> None:
        with httpx.Client(base_url=server.url, timeout=30) as own:
            barrier.wait()
            response = own.post(
                f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
            )
            codes.append(response.status_code)

    threads = [threading.Thread(target=approve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(codes) == [200] + [409] * 7
    assert employee_title(db) == "Senior Engineer"
    assert query(db, "SELECT COUNT(*) FROM events WHERE event_type = 'approval.approved'") == [(1,)]
    assert query(
        db, "SELECT COUNT(*) FROM company_idempotency WHERE operation = 'update_employee'"
    ) == [(1,)]

    with httpx.Client(base_url=server.url, timeout=30) as client:
        before = client.get(f"/api/v1/tasks/{task_id}/history").json()
        boot = client.get("/api/v1/health").json()["boot_id"]

    server.restart()

    with httpx.Client(base_url=server.url, timeout=30) as client:
        assert client.get("/api/v1/health").json()["boot_id"] != boot
        assert client.get(f"/api/v1/tasks/{task_id}/history").json() == before
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        assert detail["status"] == "completed"
        verification = client.get(f"/api/v1/tasks/{task_id}/verification").json()
        assert verification["status"] == "passed"

    status = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "status", task_id],
        cwd=tmp_path,
        env=base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["task"]["status"] == "completed"


def test_cli_created_task_can_be_approved_over_http(server: LiveServer, tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "run", EMPLOYEE_NOTE_GOAL],
        cwd=tmp_path,
        env=base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 4, run.stderr
    task_id = json.loads(run.stdout)["task"]["task_id"]
    with httpx.Client(base_url=server.url, timeout=30) as client:
        listed = [t["task_id"] for t in client.get("/api/v1/tasks").json()["tasks"]]
        assert task_id in listed
        response = client.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"


def test_injected_failure_recovery_over_http(server: LiveServer, tmp_path: Path) -> None:
    with httpx.Client(base_url=server.url, timeout=30) as client:
        created = client.post(
            "/api/v1/tasks", json={"goal": TICKET_NOTE_GOAL, "inject_failure": "create_ticket"}
        )
        done = wait_status(client, created.json()["task_id"], {"completed", "failed"})
        types = [e["event_type"] for e in client.get(
            f"/api/v1/tasks/{done['task_id']}/history"
        ).json()["events"]]
    assert done["status"] == "completed" and done["retry_count"] == 1
    assert types.count("recovery.retry_started") == 1
    assert types.count("recovery.retry_succeeded") == 1
    assert (tmp_path / "workspace" / "acme_billing_summary.txt").exists()
    assert query(tmp_path / "data" / "runtime.db", "SELECT COUNT(*) FROM company_tickets") == [(1,)]


def test_public_mode_over_http(tmp_path: Path) -> None:
    live = LiveServer(
        tmp_path,
        {
            "APP_MODE": "public",
            "ALLOWED_HOSTS": "127.0.0.1",
            "PUBLIC_DATA_ROOT": str(tmp_path / "public"),
            "SESSION_COOKIE_SECURE": "false",
        },
    ).start()
    try:
        with httpx.Client(base_url=live.url, timeout=30) as anon:
            assert anon.get("/api/v1/tasks").status_code == 401
            assert anon.post("/api/v1/tasks", json={"goal": TICKET_NOTE_GOAL}).status_code == 401
        with httpx.Client(base_url=live.url, timeout=30) as alice, httpx.Client(
            base_url=live.url, timeout=30
        ) as bob:
            assert alice.post("/api/v1/session", json={}).status_code == 201
            assert bob.post("/api/v1/session", json={}).status_code == 201
            task_id = alice.post("/api/v1/tasks", json={"goal": EMPLOYEE_NOTE_GOAL}).json()[
                "task_id"
            ]
            wait_status(alice, task_id, {"needs_approval"})
            stolen = bob.post(
                f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
            )
            assert stolen.status_code == 404
            assert alice.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"
            assert bob.get("/api/v1/tasks").json()["total"] == 0
        assert not (tmp_path / "data").exists()
    finally:
        live.stop()
