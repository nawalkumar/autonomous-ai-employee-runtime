"""Helpers shared by the HTTP API tests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

TICKET_GOAL = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
TICKET_NOTE_GOAL = (
    "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
    "and save a summary note."
)
EMPLOYEE_GOAL = "Update employee E-17 title to Senior Engineer."
EMPLOYEE_NOTE_GOAL = (
    "Update employee E-17 title to Senior Engineer and confirm the change in a note file."
)
TASK_ID = r"task_[0-9a-f]{32}"


def wait_for(
    client: TestClient,
    task_id: str,
    statuses: set[str],
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in statuses and not detail["executing"]:
            return detail
        assert time.monotonic() < deadline, f"task stuck in {detail['status']}"
        time.sleep(0.05)


def submit(client: TestClient, goal: str, **extra: Any) -> dict[str, Any]:
    headers = extra.pop("headers", None)
    response = client.post("/api/v1/tasks", json={"goal": goal, **extra}, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def query(db: Path, sql: str, *params: Any) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def employee_title(db: Path, employee_id: str = "E-17") -> str:
    return query(db, "SELECT title FROM company_employees WHERE employee_id = ?", employee_id)[0][0]


def event_types(client: TestClient, task_id: str) -> list[str]:
    history = client.get(f"/api/v1/tasks/{task_id}/history").json()
    return [e["event_type"] for e in history["events"]]
