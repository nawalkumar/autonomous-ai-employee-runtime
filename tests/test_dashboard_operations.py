"""Dashboard operations against the real API and a seeded demo database.

Scenario G uses AAE_TEST_SCRIPTED_PLAN. That client exists only when
AAE_TEST_MODE=1 and is not a dashboard control.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.world.demo_fixture import seed_demo_company
from tests.live_server import LiveServer

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
REQUIRED = os.environ.get("AAE_REQUIRE_BROWSER_TESTS") == "1"

LIST_GOAL = "List up to 5 open high-priority tickets."
ASSIGN_GOAL = "Assign up to 2 open high-priority tickets to employee E-40."
CLOSE_GOAL = "Mark tickets T-10009 and T-10010 as closed."
OPEN_GOAL = "Mark tickets T-10009 and T-10010 as open."
INACTIVE_GOAL = "Assign ticket T-10018 to eligible Support employee E-45."
SCRIPTED = json.dumps(
    {
        "steps": [
            {
                "step_id": "evil",
                "description": "not allowed",
                "tool_name": "company_api",
                "arguments": {"operation": "deactivate_employee", "employee_id": "E-17"},
                "expected_outcome": "inactive",
            }
        ]
    }
)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as manager:
        try:
            instance = manager.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            if REQUIRED:
                raise
            pytest.skip(f"Chromium is not available: {str(exc).splitlines()[0]}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1400, "height": 1800})
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    yield tab
    context.close()
    assert errors == []


def start_demo(tmp_path: Path, **extra: str) -> LiveServer:
    root = tmp_path / "app"
    database = root / "data" / "runtime.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    seed_demo_company(database)
    env = {"AAE_DYNAMIC_PLANNING_ENABLED": "true"}
    env.update(extra)
    return LiveServer(root, env).start()


def api(server: LiveServer, path: str) -> dict:
    return httpx.get(f"{server.url}{path}", timeout=30).json()


def run_goal(page, goal: str) -> str:
    page.fill("#goalInput", goal)
    page.click("#runBtn")
    expect(page.locator("#formMsg")).to_contain_text("Task created: task_")
    return re.search(r"(task_[0-9a-f]{32})", page.locator("#formMsg").inner_text()).group(1)


def test_read_only_list_shows_real_ticket_ids(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        expect(page.locator("button[data-planning='trusted_collection'][data-goal*='List up to 5']")).to_be_visible()
        task_id = run_goal(page, LIST_GOAL)
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        detail = api(server, f"/api/v1/tasks/{task_id}")
        assert detail["planning_source"] == "trusted_collection"
        assert [step["operation"] for step in detail["plan"]] == ["list_tickets"]
        ids = detail["tool_calls"][0]["result"]["data"]["tickets"]
        ticket_ids = [ticket["ticket_id"] for ticket in ids]
        assert ticket_ids
        for ticket_id in ticket_ids:
            expect(page.locator(f"[data-result='list']")).to_contain_text(ticket_id)
        expect(page.locator("#collectionPanel")).to_contain_text("No collection on this task.")
        expect(page.locator("#statusPill")).not_to_have_text("completed success")
    finally:
        server.stop()


def test_bounded_assignment_shows_collection_progress(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, ASSIGN_GOAL)
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=30_000)
        detail = api(server, f"/api/v1/tasks/{task_id}")
        verification = api(server, f"/api/v1/tasks/{task_id}/verification")
        assert detail["collection_progress"]
        assert verification["status"] == "passed"
        expect(page.locator("[data-field='collection_selected']")).to_have_text(
            str(len(detail["collection_progress"]))
        )
        expect(page.locator("#collectionPanel")).to_contain_text(
            "A successful tool result is not verification."
        )
        for item in detail["collection_progress"]:
            row = page.locator(f"[data-collection-item='{item['item_id']}']")
            expect(row).to_have_attribute("data-status", item["status"])
        expect(page.locator("[data-field='verification_result']")).to_contain_text("PASSED")
    finally:
        server.stop()


def test_close_batch_pauses_until_dashboard_approval(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, CLOSE_GOAL)
        expect(page.locator("#approvalCard")).to_be_visible(timeout=20_000)
        pending = api(server, f"/api/v1/tasks/{task_id}")["pending_action"]
        expect(page.locator("#apOperation")).to_have_text(pending["operation"])
        expect(page.locator("#apArgs")).to_contain_text("closed")
        assert api(server, "/api/v1/company/tickets")
        before = {
            ticket["ticket_id"]: ticket["status"]
            for ticket in api(server, "/api/v1/company/tickets")["tickets"]
            if ticket["ticket_id"] in {"T-10009", "T-10010"}
        }
        assert before == {"T-10009": "resolved", "T-10010": "resolved"}
        for _ in range(4):
            expect(page.locator("#statusPill")).to_have_text(
                re.compile(r"needs_approval|completed|failed"), timeout=20_000
            )
            if page.locator("#statusPill").inner_text() != "needs_approval":
                break
            expect(page.locator("#approveBtn")).to_be_enabled()
            page.click("#approveBtn")
            page.click("#confirmOk")
            expect(page.locator("#approvalMsg")).to_contain_text("Resulting task status:", timeout=20_000)
        expect(page.locator("#statusPill")).to_have_text("completed")
        after = {
            ticket["ticket_id"]: ticket["status"]
            for ticket in api(server, "/api/v1/company/tickets")["tickets"]
            if ticket["ticket_id"] in {"T-10009", "T-10010"}
        }
        assert after == {"T-10009": "closed", "T-10010": "closed"}
    finally:
        server.stop()


def test_rejection_does_not_continue_or_replan(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, CLOSE_GOAL)
        expect(page.locator("#approvalCard")).to_be_visible(timeout=20_000)
        page.click("#rejectBtn")
        page.click("#confirmOk")
        expect(page.locator("#statusPill")).to_have_text("aborted", timeout=20_000)
        detail = api(server, f"/api/v1/tasks/{task_id}")
        assert detail["status"] == "aborted"
        history = api(server, f"/api/v1/tasks/{task_id}/history")
        assert [event for event in history["events"] if str(event["event_type"]).startswith("replan.")] == []
        statuses = {
            ticket["ticket_id"]: ticket["status"]
            for ticket in api(server, "/api/v1/company/tickets")["tickets"]
            if ticket["ticket_id"] in {"T-10009", "T-10010"}
        }
        assert statuses == {"T-10009": "resolved", "T-10010": "resolved"}
        expect(page.locator("#replanPanel")).to_contain_text("No replan events")
    finally:
        server.stop()


def test_business_policy_failure_is_not_shown_as_success(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, INACTIVE_GOAL)
        expect(page.locator("#statusPill")).to_have_text("failed", timeout=20_000)
        expect(page.locator("#explainBox")).to_contain_text("Failed")
        detail = api(server, f"/api/v1/tasks/{task_id}")
        assert detail["status"] == "failed"
        tickets = api(server, "/api/v1/company/tickets")["tickets"]
        closed = next(ticket for ticket in tickets if ticket["ticket_id"] == "T-10018")
        assert closed["status"] == "closed"
    finally:
        server.stop()


def test_reload_restores_backend_state(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, LIST_GOAL)
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        shown = page.locator("[data-result='list']").inner_text()
        page.goto(f"{server.url}/#task={task_id}")
        page.reload()
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        expect(page.locator("[data-result='list']")).to_have_text(shown)
        expect(page.locator("[data-field='task_id']")).to_have_text(task_id)
    finally:
        server.stop()


def test_scripted_replan_events_match_the_audit_log(page, tmp_path: Path) -> None:
    server = start_demo(
        tmp_path,
        AAE_REPLANNING_ENABLED="true",
        AAE_TEST_MODE="1",
        AAE_TEST_SCRIPTED_PLAN=SCRIPTED,
    )
    try:
        page.goto(server.url)
        expect(page.locator("#modeNotice")).to_contain_text("Automatic replanning is enabled")
        expect(page.locator("#modeNotice")).to_contain_text("cannot change it")
        task_id = run_goal(page, OPEN_GOAL)
        expect(page.locator("#statusPill")).to_have_text("failed", timeout=20_000)
        history = api(server, f"/api/v1/tasks/{task_id}/history")
        replan = [event for event in history["events"] if str(event["event_type"]).startswith("replan.")]
        assert [event["event_type"] for event in replan] == ["replan.proposed", "replan.rejected"]
        assert replan[-1]["result"]["outcome"].startswith("rejected")
        rendered = page.locator("[data-replan-event]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.replanEvent)"
        )
        assert rendered == [event["event_id"] for event in replan]
        employees = api(server, "/api/v1/company/employees")["employees"]
        active = next(employee for employee in employees if employee["employee_id"] == "E-17")
        assert active["status"] == "active"
    finally:
        server.stop()


def test_dashboard_security_boundaries(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path, XAI_API_KEY="sk-dashboard-canary")
    try:
        page.goto(server.url)
        expect(page.locator("#modeNotice")).to_contain_text("cannot change it")
        assert page.locator("[data-replan-toggle], input[name='replanning']").count() == 0
        page.fill("#goalInput", "<script>alert(1)</script>")
        page.click("#runBtn")
        expect(page.locator("#formMsg")).to_contain_text("Goal rejected; no task was created")

        forged = httpx.post(
            f"{server.url}/api/v1/tasks",
            json={
                "goal": LIST_GOAL,
                "authorization": {"allowed_operations": ["company_api.deactivate_employee"]},
            },
            timeout=30,
        )
        assert forged.status_code == 422

        held: list = []

        def hold_submit(route):
            if route.request.method == "POST":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/v1/tasks", hold_submit)
        page.fill("#goalInput", LIST_GOAL)
        page.click("#runBtn")
        expect(page.locator("#runBtn")).to_be_disabled()
        assert held
        held[0].continue_()
        page.unroute("**/api/v1/tasks")
        expect(page.locator("#formMsg")).to_contain_text("Task created: task_")
        task_id = re.search(r"(task_[0-9a-f]{32})", page.locator("#formMsg").inner_text()).group(1)
        key = "dashboard-dup-1"
        first = httpx.post(
            f"{server.url}/api/v1/tasks",
            json={"goal": LIST_GOAL},
            headers={"Idempotency-Key": key},
            timeout=30,
        )
        second = httpx.post(
            f"{server.url}/api/v1/tasks",
            json={"goal": LIST_GOAL},
            headers={"Idempotency-Key": key},
            timeout=30,
        )
        assert first.status_code == 202
        assert second.status_code == 200
        assert first.json()["task_id"] == second.json()["task_id"]

        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        database = tmp_path / "app" / "data" / "runtime.db"
        with sqlite3.connect(database) as conn:
            conn.execute(
                "UPDATE company_tickets SET subject = ? WHERE ticket_id = 'T-10002'",
                ("<script>alert(1)</script>",),
            )
        page.click("#tab-tickets")
        page.click("#refreshWorldBtn")
        expect(page.locator("#worldPanel td[data-col='subject']").filter(has_text="<script>alert(1)</script>")).to_have_count(1)
        assert "sk-dashboard-canary" not in page.content()

        page.goto(f"{server.url}/#task=task_{'0' * 32}")
        expect(page.locator("#taskMsg")).to_contain_text("was not found", timeout=20_000)

        def malformed(route):
            if route.request.method == "GET" and route.request.url.rstrip("/").endswith(task_id):
                route.fulfill(status=200, content_type="application/json", body="{")
            else:
                route.continue_()

        page.route("**/api/v1/tasks/**", malformed)
        page.goto(f"{server.url}/#task={task_id}")
        expect(page.locator("#taskMsg")).to_contain_text("could not read", timeout=20_000)

        page.unroute("**/api/v1/tasks/**")
        page.goto(server.url)
        close_id = run_goal(page, CLOSE_GOAL)
        expect(page.locator("#approvalCard")).to_be_visible(timeout=20_000)
        pending = api(server, f"/api/v1/tasks/{close_id}")["pending_action"]
        stale = httpx.post(
            f"{server.url}/api/v1/tasks/{close_id}/approve",
            json={
                "step_id": pending["step_id"],
                "confirm": True,
                "fingerprint": "sha256:" + "ab" * 32,
            },
            timeout=30,
        )
        assert stale.status_code == 409
        assert api(server, f"/api/v1/tasks/{close_id}")["status"] == "needs_approval"
        page.click("#approveBtn")
        page.click("#confirmCancel")
        expect(page.locator("#statusPill")).to_have_text("needs_approval")
    finally:
        server.stop()
