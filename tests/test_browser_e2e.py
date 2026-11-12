"""Browser end-to-end tests: the dashboard in Chromium against a live server.

They compare what the page shows with what the API returns. When Chromium cannot
be launched they are skipped, unless AAE_REQUIRE_BROWSER_TESTS=1 (set in CI),
in which case that is a failure.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest

from tests.api_support import EMPLOYEE_GOAL, TICKET_NOTE_GOAL, employee_title
from tests.live_server import LiveServer

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
REQUIRED = os.environ.get("AAE_REQUIRE_BROWSER_TESTS") == "1"


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - missing browser or system libraries
            if REQUIRED:
                raise
            pytest.skip(f"Chromium is not available: {str(exc).splitlines()[0]}")
        yield instance
        instance.close()


@pytest.fixture
def server(tmp_path: Path):
    live = LiveServer(tmp_path).start()
    yield live
    live.stop()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1400, "height": 1600})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert errors == []


def api(server: LiveServer, path: str) -> dict:
    return httpx.get(f"{server.url}{path}", timeout=30).json()


def field(page, container: str, name: str):
    return page.locator(f"#{container} [data-field='{name}']")


def run_goal(page, goal: str, inject: bool = False) -> str:
    page.fill("#goalInput", goal)
    if inject:
        page.check("#injectToggle")
    page.click("#runBtn")
    expect(page.locator("#formMsg")).to_contain_text("Task created: task_")
    return re.search(r"(task_[0-9a-f]{32})", page.locator("#formMsg").inner_text()).group(1)


def test_ticket_with_injected_failure_matches_the_backend(page, server: LiveServer) -> None:
    page.goto(server.url)
    expect(page.locator("#modeBadge")).to_have_text("Live · local mode")
    page.click("button[data-goal*='summary note']")
    expect(page.locator("#injectRow")).to_be_visible()
    task_id = run_goal(page, TICKET_NOTE_GOAL, inject=True)

    expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
    expect(page.locator("[data-check-name]").first).to_be_visible()
    detail = api(server, f"/api/v1/tasks/{task_id}")
    history = api(server, f"/api/v1/tasks/{task_id}/history")
    verification = api(server, f"/api/v1/tasks/{task_id}/verification")

    expect(field(page, "runtimeState", "task_id")).to_have_text(task_id)
    expect(field(page, "runtimeState", "retry_count")).to_have_text(str(detail["retry_count"]))
    expect(page.locator("#mRecovery")).to_have_text(str(detail["retry_count"]))
    expect(page.locator("#mVerify")).to_have_text(
        f"PASSED {verification['checks_passed']}/{verification['checks_total']}"
    )
    expect(page.locator("#mEvidence")).to_have_text(f"{detail['evidence_count']} items")
    expect(page.locator("#eventCount")).to_have_text(f"{history['count']} events")
    expect(page.locator(".tl-item")).to_have_count(history["count"])
    rendered = page.locator(".tl-item").evaluate_all(
        "items => items.map(i => i.dataset.eventId)"
    )
    assert rendered == [e["event_id"] for e in history["events"]]
    expect(page.locator(".tl-item[data-event-type='demo.failure_injected'] .tag.sim")).to_be_visible()
    expect(page.locator(".tl-item[data-event-type='recovery.retry_started']")).to_have_count(1)
    expect(page.locator("[data-check-name]")).to_have_count(verification["checks_total"])
    for step in detail["plan"]:
        expect(page.locator(f"#planList li[data-step-id='{step['step_id']}']")).to_have_attribute(
            "data-status", step["status"]
        )
    expect(page.locator(".stage[data-stage='Adapt']")).to_have_attribute("data-state", "success")

    page.click("#tab-tickets")
    ticket = api(server, "/api/v1/company/tickets")["tickets"][0]
    expect(page.locator("#worldPanel td[data-col='ticket_id']")).to_have_text([ticket["ticket_id"]])
    page.click("#tab-files")
    page.click("button[aria-label='View acme_billing_summary.txt']")
    content = api(server, "/api/v1/workspace/file?path=acme_billing_summary.txt")["content"]
    expect(page.locator("[data-file-content='acme_billing_summary.txt']")).to_have_text(content)


def test_approval_through_the_dashboard(page, server: LiveServer, tmp_path: Path) -> None:
    page.goto(server.url)
    task_id = run_goal(page, EMPLOYEE_GOAL)
    expect(page.locator("#approvalCard")).to_be_visible(timeout=20_000)
    pending = api(server, f"/api/v1/tasks/{task_id}")["pending_action"]
    expect(page.locator("#apStep")).to_have_text(f"{pending['step_id']} · {pending['tool']}")
    expect(page.locator("#apOperation")).to_have_text(pending["operation"])
    expect(page.locator("#apRisk")).to_have_text(pending["risk"])
    expect(page.locator("#apReason")).to_have_text(pending["reason"])
    assert employee_title(tmp_path / "data" / "runtime.db") == "Software Engineer"

    expect(page.locator("#approvalCard")).to_have_attribute("data-approval-ready", "1", timeout=20_000)
    expect(page.locator("#approveBtn")).to_be_enabled()
    page.locator("#approveBtn").click()
    expect(page.locator("#confirmDialog")).to_be_visible()
    page.locator("#confirmCancel").click()
    expect(page.locator("#statusPill")).to_have_text("needs_approval")
    assert api(server, f"/api/v1/tasks/{task_id}")["status"] == "needs_approval"

    expect(page.locator("#approvalCard")).to_have_attribute("data-approval-ready", "1")
    page.locator("#approveBtn").click()
    page.locator("#confirmOk").click()
    expect(page.locator("#statusPill")).to_have_text("completed", timeout=20_000)
    expect(page.locator("#approvalMsg")).to_contain_text("Resulting task status: completed")
    assert employee_title(tmp_path / "data" / "runtime.db") == "Senior Engineer"
    page.click("#tab-employees")
    expect(page.locator("#worldPanel tr:has(td[data-col='employee_id']:text-is('E-17')) td[data-col='title']")).to_have_text("Senior Engineer")
    expect(page.locator(".tl-item[data-event-type='approval.approved']")).to_contain_text(
        "approved by local-dashboard"
    )


def test_rejection_through_the_dashboard(page, server: LiveServer, tmp_path: Path) -> None:
    page.goto(server.url)
    task_id = run_goal(page, EMPLOYEE_GOAL)
    expect(page.locator("#approvalCard")).to_have_attribute("data-approval-ready", "1", timeout=20_000)
    page.fill("#rejectReason", "Budget freeze")
    expect(page.locator("#rejectBtn")).to_be_enabled()
    page.locator("#rejectBtn").click()
    page.locator("#confirmOk").click()
    expect(page.locator("#statusPill")).to_have_text("aborted", timeout=20_000)
    detail = api(server, f"/api/v1/tasks/{task_id}")
    assert detail["status"] == "aborted" and detail["tool_calls"] == []
    assert employee_title(tmp_path / "data" / "runtime.db") == "Software Engineer"
    expect(page.locator("#explainBox")).to_contain_text("Budget freeze")
    expect(page.locator("#approvalCard")).to_be_hidden()


def test_unsupported_goal_is_rejected_by_the_server(page, server: LiveServer) -> None:
    page.goto(server.url)
    page.fill("#goalInput", "Delete every employee record")
    expect(page.locator("#goalHint")).to_contain_text("Not one of the listed goals")
    page.click("#runBtn")
    expect(page.locator("#formMsg")).to_contain_text("Goal rejected; no task was created")
    assert api(server, "/api/v1/tasks")["total"] == 0


def test_history_reload_and_backend_outage(page, server: LiveServer) -> None:
    page.goto(server.url)
    task_id = run_goal(page, EMPLOYEE_GOAL)
    expect(page.locator("#statusPill")).to_have_text("needs_approval", timeout=20_000)

    server.restart()
    page.goto(f"{server.url}/#task={task_id}")
    expect(page.locator("#statusPill")).to_have_text("needs_approval")
    expect(page.locator(f"#taskList button[data-task-id='{task_id}']")).to_have_attribute(
        "aria-current", "true"
    )
    expect(page.locator(".tl-item")).to_have_count(api(server, f"/api/v1/tasks/{task_id}/history")["count"])

    server.stop()
    page.fill("#goalInput", TICKET_NOTE_GOAL)
    page.click("#runBtn")
    expect(page.locator("#formMsg")).to_contain_text("The backend did not answer")


def test_public_sessions_in_separate_browsers(browser, tmp_path: Path) -> None:
    live = LiveServer(
        tmp_path,
        {
            "APP_MODE": "public",
            "ALLOWED_HOSTS": "127.0.0.1",
            "PUBLIC_DATA_ROOT": str(tmp_path / "public"),
            "SESSION_COOKIE_SECURE": "false",
        },
    ).start()
    alice_ctx = browser.new_context()
    bob_ctx = browser.new_context()
    try:
        alice = alice_ctx.new_page()
        alice.goto(live.url)
        expect(alice.locator("#sessionCard")).to_be_visible()
        expect(alice.locator("#dashboard")).to_be_hidden()
        expect(alice.locator("#modeNotice")).to_contain_text("erased")
        alice.click("#sessionBtn")
        expect(alice.locator("#dashboard")).to_be_visible()
        task_id = run_goal(alice, EMPLOYEE_GOAL)
        expect(alice.locator("#statusPill")).to_have_text("needs_approval", timeout=20_000)

        bob = bob_ctx.new_page()
        bob.goto(f"{live.url}/#task={task_id}")
        bob.click("#sessionBtn")
        expect(bob.locator("#taskMsg")).to_contain_text("was not found")
        expect(bob.locator("#taskList")).to_contain_text("No tasks yet")
    finally:
        alice_ctx.close()
        bob_ctx.close()
        live.stop()
