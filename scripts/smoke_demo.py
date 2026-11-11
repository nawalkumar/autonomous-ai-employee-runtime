"""Smoke-test a running deployment over real HTTP.

    uv run python scripts/smoke_demo.py https://<service>.onrender.com [--access-code CODE]
    uv run python scripts/smoke_demo.py http://127.0.0.1:8000 --allow-local

Runs every demo scenario against the server and checks the outcomes through the
API. Against a local-mode server it writes real tasks into the local database,
so it requires --allow-local. Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

TICKET = (
    "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
    "and save a summary note."
)
EMPLOYEE = "Update employee E-17 title to Senior Engineer and confirm the change in a note file."


class SmokeFailure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def wait(client: httpx.Client, task_id: str, statuses: set[str], timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        if detail["status"] in statuses and not detail["executing"]:
            return detail
        if time.monotonic() > deadline:
            raise SmokeFailure(f"task {task_id} stuck in {detail['status']}")
        time.sleep(0.5)


def submit(client: httpx.Client, goal: str, **extra) -> dict:
    response = client.post(
        "/api/v1/tasks",
        json={"goal": goal, **extra},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    check(response.status_code == 202, f"submitted goal (HTTP {response.status_code})")
    return response.json()


def run(base_url: str, access_code: str | None, allow_local: bool) -> None:
    with httpx.Client(base_url=base_url, timeout=90, follow_redirects=False) as client:
        started = time.monotonic()
        health = None
        while health is None or health.status_code != 200:
            try:
                health = client.get("/api/v1/health")
            except httpx.TransportError:
                health = None
            if time.monotonic() - started > 180:
                break
            if health is None or health.status_code != 200:
                time.sleep(3)
        check(health is not None, "server reachable")
        check(health.status_code == 200, f"health ({time.monotonic() - started:.1f}s incl. cold start)")
        info = health.json()
        print(f"      mode={info['mode']} version={info['version']} persistence={info['persistence']}")
        if info["mode"] == "local" and not allow_local:
            raise SmokeFailure("server is in local mode; pass --allow-local to write real tasks")

        if info["mode"] == "public":
            check(client.get("/api/v1/tasks").status_code == 401, "no access without a session")
            body = {"access_code": access_code} if access_code else {}
            session = client.post("/api/v1/session", json=body)
            check(session.status_code == 201, f"demo session started (HTTP {session.status_code})")

        page = client.get("/")
        check(page.status_code == 200 and "app.js" in page.text, "dashboard served")
        check("content-security-policy" in page.headers, "CSP header present")

        print("Ticket with simulated 503:")
        task = submit(client, TICKET, inject_failure="create_ticket")
        done = wait(client, task["task_id"], {"completed", "failed"})
        check(done["status"] == "completed", "task completed")
        check(done["retry_count"] == 1, "one bounded retry")
        verification = client.get(f"/api/v1/tasks/{task['task_id']}/verification").json()
        check(verification["status"] == "passed", f"verification {verification['checks_passed']}/{verification['checks_total']}")
        types = [e["event_type"] for e in client.get(f"/api/v1/tasks/{task['task_id']}/history").json()["events"]]
        check("recovery.retry_succeeded" in types, "retry recorded in history")
        files = [f["path"] for f in client.get("/api/v1/workspace/files").json()["files"]]
        check("acme_billing_summary.txt" in files, "summary note in workspace")

        print("Approval:")
        task = submit(client, EMPLOYEE)
        paused = wait(client, task["task_id"], {"needs_approval"})
        check(paused["pending_action"]["operation"] == "update_employee", "paused before update_employee")
        approved = client.post(
            f"/api/v1/tasks/{task['task_id']}/approve", json={"step_id": "step_1", "confirm": True}
        )
        check(approved.status_code == 200 and approved.json()["status"] == "completed", "approved and completed")
        e17 = next(e for e in client.get("/api/v1/company/employees").json()["employees"] if e["employee_id"] == "E-17")
        check(e17["title"] == "Senior Engineer", "E-17 title updated")

        print("Rejection:")
        task = submit(client, EMPLOYEE)
        wait(client, task["task_id"], {"needs_approval"})
        rejected = client.post(
            f"/api/v1/tasks/{task['task_id']}/reject",
            json={"step_id": "step_1", "reason": "smoke test", "confirm": True},
        )
        check(rejected.json()["status"] == "aborted" and rejected.json()["tool_calls"] == [], "rejected; write not executed")

        print("Guards:")
        check(client.post("/api/v1/tasks", json={"goal": "Delete all employees"}).status_code == 422, "unsupported goal rejected")
        check(client.get("/api/v1/workspace/file", params={"path": "../data/runtime.db"}).status_code == 400, "path traversal denied")
        cross = client.post("/api/v1/tasks", json={"goal": TICKET}, headers={"Origin": "https://evil.example"})
        check(cross.status_code == 403, "cross-origin mutation refused")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--access-code")
    parser.add_argument("--allow-local", action="store_true")
    args = parser.parse_args()
    try:
        run(args.base_url.rstrip("/"), args.access_code, args.allow_local)
    except (SmokeFailure, httpx.HTTPError, KeyError, StopIteration) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
