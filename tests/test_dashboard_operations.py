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


COLLECTION_STATUSES = (
    "pending",
    "running",
    "awaiting_approval",
    "completed",
    "verified",
    "failed",
    "rejected",
    "skipped",
)


def expect_collection_matches_api(page, detail: dict) -> None:
    """Every counter is counted from the API rows, not from a tool result."""
    items = detail["collection_progress"]
    counts = {status: 0 for status in COLLECTION_STATUSES}
    for item in items:
        assert item["status"] in counts
        counts[item["status"]] += 1
    processed = sum(
        1 for item in items if item["status"] not in {"pending", "running"}
    )
    expect(page.locator("[data-field='collection_selected']")).to_have_text(str(len(items)))
    expect(page.locator("[data-field='collection_total']")).to_have_text(str(len(items)))
    expect(page.locator("[data-field='collection_processed']")).to_have_text(str(processed))
    for status, count in counts.items():
        expect(page.locator(f"[data-collection-count='{status}']")).to_have_text(
            f"{status} {count}"
        )
    for item in items:
        row = page.locator(f"[data-collection-item='{item['item_id']}']")
        expect(row).to_have_attribute("data-status", item["status"])


def expect_approval(page, step_id: str, label: str) -> None:
    expect(page.locator(f"#planList li[data-step-id='{step_id}']")).to_have_attribute(
        "data-approval", label
    )


def wait_until_approvable(page, server: LiveServer, task_id: str) -> dict:
    """Sync on backend pending approval and the matching enabled dashboard control."""
    expect(page.locator("#approvalCard")).to_have_attribute(
        "data-approval-ready", "1", timeout=20_000
    )
    expect(page.locator("#statusPill")).to_have_text("needs_approval")
    pending = api(server, f"/api/v1/tasks/{task_id}")["pending_action"]
    assert pending is not None
    assert pending.get("step_id") and pending.get("fingerprint")
    expect(page.locator("#approvalCard")).to_have_attribute(
        "data-pending-step", pending["step_id"]
    )
    expect(page.locator("#approvalCard")).to_have_attribute(
        "data-pending-fingerprint", pending["fingerprint"]
    )
    expect(page.locator("#approveBtn")).to_be_enabled()
    expect(page.locator("#approveBtn")).to_be_visible()
    return pending


def approve_through_dashboard(page, server: LiveServer, task_id: str) -> dict:
    """Approve the current pending step only after UI and API agree on the target."""
    pending = wait_until_approvable(page, server, task_id)
    page.locator("#approveBtn").click()
    page.locator("#confirmOk").click()
    expect(page.locator("#approvalMsg")).to_contain_text(
        "Resulting task status:", timeout=20_000
    )
    return pending


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
        for step in detail["plan"]:
            expect_approval(page, step["step_id"], "Not required.")
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
        expect_collection_matches_api(page, detail)
        assert any(item["status"] == "verified" for item in detail["collection_progress"])
        assert all(item["status"] != "completed" for item in detail["collection_progress"])
        expect(page.locator("#collectionPanel")).to_contain_text(
            "A successful tool result is not verification."
        )
        for step in detail["plan"]:
            expect_approval(page, step["step_id"], "Not required.")
        expect(page.locator("[data-field='verification_result']")).to_contain_text("PASSED")
    finally:
        server.stop()


def test_close_batch_pauses_until_dashboard_approval(page, tmp_path: Path) -> None:
    server = start_demo(tmp_path)
    try:
        page.goto(server.url)
        task_id = run_goal(page, CLOSE_GOAL)
        pending = wait_until_approvable(page, server, task_id)
        expect(page.locator("#apOperation")).to_have_text(pending["operation"])
        expect(page.locator("#apArgs")).to_contain_text("closed")
        plan = api(server, f"/api/v1/tasks/{task_id}")["plan"]
        for step in plan:
            label = "Awaiting approval." if step["step_id"] == pending["step_id"] else "Not required."
            expect_approval(page, step["step_id"], label)
        assert api(server, "/api/v1/company/tickets")
        before = {
            ticket["ticket_id"]: ticket["status"]
            for ticket in api(server, "/api/v1/company/tickets")["tickets"]
            if ticket["ticket_id"] in {"T-10009", "T-10010"}
        }
        assert before == {"T-10009": "resolved", "T-10010": "resolved"}
        seen_fingerprints: list[str] = []
        for _ in range(4):
            detail = api(server, f"/api/v1/tasks/{task_id}")
            if detail["status"] != "needs_approval":
                break
            approved = approve_through_dashboard(page, server, task_id)
            assert approved["fingerprint"] not in seen_fingerprints
            seen_fingerprints.append(approved["fingerprint"])
        assert seen_fingerprints, "expected at least one dashboard approval"
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        expect(page.locator("#approvalCard")).to_have_attribute("data-approval-ready", "0")
        finished = api(server, f"/api/v1/tasks/{task_id}")
        for step in finished["plan"]:
            expect_approval(page, step["step_id"], "Approved.")
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
        pending = wait_until_approvable(page, server, task_id)
        expect(page.locator("#rejectBtn")).to_be_enabled()
        page.locator("#rejectBtn").click()
        page.locator("#confirmOk").click()
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
        for step in detail["plan"]:
            label = "Rejected." if step["step_id"] == pending["step_id"] else "Not required."
            expect_approval(page, step["step_id"], label)
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
        pending = wait_until_approvable(page, server, close_id)
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
        wait_until_approvable(page, server, close_id)
        page.locator("#approveBtn").click()
        page.locator("#confirmCancel").click()
        expect(page.locator("#statusPill")).to_have_text("needs_approval")
        expect(page.locator("#approvalCard")).to_have_attribute("data-approval-ready", "1")
        expect_approval(page, pending["step_id"], "Awaiting approval.")
    finally:
        server.stop()


def test_empty_assignment_is_not_shown_as_verified(page, tmp_path: Path) -> None:
    root = tmp_path / "app"
    database = root / "data" / "runtime.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    seed_demo_company(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE company_tickets SET status = 'resolved' "
            "WHERE status = 'open' AND priority = 'high'"
        )
    server = LiveServer(root, {"AAE_DYNAMIC_PLANNING_ENABLED": "true"}).start()
    try:
        page.goto(server.url)
        task_id = run_goal(page, ASSIGN_GOAL)
        expect(page.locator("#statusPill")).to_have_text("failed", timeout=20_000)
        detail = api(server, f"/api/v1/tasks/{task_id}")
        verification = api(server, f"/api/v1/tasks/{task_id}/verification")
        assert detail["status"] == "failed"
        assert verification["status"] != "passed"
        if not detail["collection_progress"]:
            expect(page.locator("#collectionPanel")).to_contain_text("No collection on this task.")
        else:
            expect_collection_matches_api(page, detail)
            assert all(item["status"] != "verified" for item in detail["collection_progress"])
    finally:
        server.stop()


def prepare_scripted_acceptance(root: str) -> str:
    """Commit one accepted revision with a scripted provider.

    This runs in a child process. Playwright already owns an event loop, and the
    executor's ``asyncio.run`` cannot share it. The provider is the test double,
    not a live model.
    """
    import app.main  # noqa: F401  stable import order before the model package
    from app.config import get_settings
    from app.models.enums import TaskStatus, VerificationStatus
    from app.models.goal import SuccessCriterion
    from tests.company_support import make_world, step
    from tests.test_automatic_replan import ScriptedLLM, _proposal, _scope

    os.environ["AAE_REPLANNING_ENABLED"] = "true"
    os.environ["AAE_MAX_REPLAN_ROUNDS"] = "2"
    get_settings.cache_clear()
    world = make_world(Path(root), demo=True)
    world.runtime._replan_llm = ScriptedLLM(_proposal("fix", "in_progress"))
    state = world.runtime.create_task(
        user_goal="scripted recovery of T-10009",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
        success_criteria=[
            SuccessCriterion(
                description="ticket moved",
                criterion_type="ticket_fields",
                expected_value={"ticket_id": "T-10009", "status": "in_progress"},
            )
        ],
    )
    final = world.runtime.run(state.task_id)
    if final.final_status is not TaskStatus.COMPLETED:
        raise RuntimeError(final.completion_summary)
    if final.verification_status is not VerificationStatus.PASSED or final.replan_count != 1:
        raise RuntimeError(f"unexpected revision outcome {final.verification_status} {final.replan_count}")
    return state.task_id


def test_dashboard_shows_scripted_accepted_replan(page, tmp_path: Path) -> None:
    """The dashboard only renders the audit rows the child process committed."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    root = tmp_path / "app"
    with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as pool:
        task_id = pool.submit(prepare_scripted_acceptance, str(root)).result()
    server = LiveServer(
        root,
        {"AAE_DYNAMIC_PLANNING_ENABLED": "true", "AAE_REPLANNING_ENABLED": "true"},
    ).start()
    try:
        page.goto(f"{server.url}/#task={task_id}")
        page.reload()
        expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
        history = api(server, f"/api/v1/tasks/{task_id}/history")
        replan = [event for event in history["events"] if str(event["event_type"]).startswith("replan.")]
        assert [event["event_type"] for event in replan] == ["replan.proposed", "replan.accepted"]
        rendered = page.locator("[data-replan-event]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.replanEvent)"
        )
        assert rendered == [event["event_id"] for event in replan]
        accepted = replan[-1]["result"]
        budgets = accepted["budgets"]
        row = page.locator(f"[data-replan-event='{replan[-1]['event_id']}']")
        expect(row).to_contain_text(f"revision {accepted['revision']}")
        expect(row).to_contain_text(str(accepted["trigger"]))
        expect(row).to_contain_text(str(accepted["outcome"]))
        expect(row).to_contain_text(f"steps {budgets['effective_steps']}/{budgets['max_steps']}")
        expect(row).to_contain_text(f"mutations remaining {budgets['mutations_remaining']}")
        ticket = next(
            item
            for item in api(server, "/api/v1/company/tickets")["tickets"]
            if item["ticket_id"] == "T-10009"
        )
        assert ticket["status"] == "in_progress"
        expect(page.locator("[data-field='verification_result']")).to_contain_text("PASSED")
    finally:
        server.stop()


def test_dashboard_shows_exhausted_replan_without_applying_a_script(page, tmp_path: Path) -> None:
    """A zero round budget is server configuration. The scripted plan must not run."""
    server = start_demo(
        tmp_path,
        AAE_REPLANNING_ENABLED="true",
        AAE_MAX_REPLAN_ROUNDS="0",
        AAE_TEST_MODE="1",
        AAE_TEST_SCRIPTED_PLAN=SCRIPTED,
    )
    try:
        page.goto(server.url)
        task_id = run_goal(page, OPEN_GOAL)
        expect(page.locator("#statusPill")).to_have_text("failed", timeout=20_000)
        detail = api(server, f"/api/v1/tasks/{task_id}")
        history = api(server, f"/api/v1/tasks/{task_id}/history")
        replan = [event for event in history["events"] if str(event["event_type"]).startswith("replan.")]
        assert [event["event_type"] for event in replan] == ["replan.exhausted"]
        assert detail["status"] == "failed"
        rendered = page.locator("[data-replan-event]").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('data-event-type'))"
        )
        assert rendered == ["replan.exhausted"]
        statuses = {
            ticket["ticket_id"]: ticket["status"]
            for ticket in api(server, "/api/v1/company/tickets")["tickets"]
            if ticket["ticket_id"] in {"T-10009", "T-10010", "E-17"}
        }
        assert statuses["T-10009"] == "resolved"
        assert statuses["T-10010"] == "resolved"
        employees = api(server, "/api/v1/company/employees")["employees"]
        active = next(employee for employee in employees if employee["employee_id"] == "E-17")
        assert active["status"] == "active"
        page.reload()
        expect(page.locator("[data-replan-event]")).to_have_attribute("data-event-type", "replan.exhausted")
        expect(page.locator("#statusPill")).to_have_text("failed")
    finally:
        server.stop()
