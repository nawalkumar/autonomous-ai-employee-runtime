"""Public demo mode: per-session sandboxes, authorization, quotas and limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.sessions import SESSION_COOKIE
from app.config import Settings
from app.main import create_app
from tests.api_support import (
    EMPLOYEE_GOAL,
    TICKET_GOAL,
    TICKET_NOTE_GOAL,
    employee_title,
    submit,
    wait_for,
)

BASE_URL = "https://testserver"


def public_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "app_mode": "public",
        "allowed_hosts": "testserver",
        "public_data_root": str(tmp_path / "public"),
        "database_path": str(tmp_path / "unused" / "runtime.db"),
        "workspace_path": str(tmp_path / "unused-workspace"),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return public_settings(tmp_path)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def new_client(app) -> TestClient:
    return TestClient(app, base_url=BASE_URL)


def start_session(client: TestClient, **body) -> dict:
    response = client.post("/api/v1/session", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def session_db(settings: Settings, client: TestClient) -> Path:
    app_ctx = client.app.state.aae
    session = app_ctx.sessions.resolve(client.cookies.get(SESSION_COOKIE))
    return Path(app_ctx.sessions.settings_for(session, settings).database_path)


def test_startup_erases_previous_demo_state(tmp_path: Path, settings: Settings) -> None:
    stale = Path(settings.public_root) / "old-session" / "data"
    stale.mkdir(parents=True)
    (stale / "runtime.db").write_text("stale", encoding="utf-8")
    with new_client(create_app(settings)) as client:
        assert client.get("/api/v1/health").json()["persistence"] == "ephemeral"
    assert not (Path(settings.public_root) / "old-session").exists()


def test_requests_without_a_session_are_refused(app, settings: Settings) -> None:
    with new_client(app) as client:
        info = client.get("/api/v1/session").json()
        assert info["mode"] == "public" and info["authenticated"] is False
        assert "erased" in info["notice"]
        for method, path, body in (
            ("get", "/api/v1/tasks", None),
            ("post", "/api/v1/tasks", {"goal": TICKET_GOAL}),
            ("get", "/api/v1/tasks/task_x", None),
            ("post", "/api/v1/tasks/task_x/approve", {"step_id": "step_1", "confirm": True}),
            ("post", "/api/v1/tasks/task_x/reject", {"step_id": "step_1", "confirm": True}),
            ("get", "/api/v1/company/employees", None),
            ("get", "/api/v1/workspace/files", None),
        ):
            response = client.request(method, path, json=body)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "session_required"
        client.cookies.set(SESSION_COOKIE, "forged-token")
        assert client.get("/api/v1/tasks").status_code == 401
    assert list(Path(settings.public_root).iterdir()) == []


META_ROUTES = {"/health", "/api/v1/health", "/api/v1/scenarios", "/api/v1/session"}
EXPECTED_ROUTES = META_ROUTES | {
    "/api/v1/tasks",
    "/api/v1/tasks/{task_id}",
    "/api/v1/tasks/{task_id}/history",
    "/api/v1/tasks/{task_id}/verification",
    "/api/v1/tasks/{task_id}/approve",
    "/api/v1/tasks/{task_id}/reject",
    "/api/v1/tasks/{task_id}/resume",
    "/api/v1/company/employees",
    "/api/v1/company/customers",
    "/api/v1/company/tickets",
    "/api/v1/workspace/files",
    "/api/v1/workspace/file",
}
MUTATION_BODIES = {
    "/api/v1/tasks": {"goal": TICKET_GOAL},
    "/api/v1/tasks/{task_id}/approve": {"step_id": "step_1", "confirm": True},
    "/api/v1/tasks/{task_id}/reject": {"step_id": "step_1", "confirm": True},
    "/api/v1/tasks/{task_id}/resume": {"confirm": True},
}


def api_routes(tmp_path: Path) -> dict[str, list[str]]:
    local = Settings(
        _env_file=None,
        database_path=str(tmp_path / "local" / "runtime.db"),
        workspace_path=str(tmp_path / "local-workspace"),
    )
    return {
        path: [method.upper() for method in ops]
        for path, ops in create_app(local).openapi()["paths"].items()
    }


def call(client: TestClient, method: str, path: str, task_id: str = "task_x"):
    return client.request(
        method,
        path.replace("{task_id}", task_id),
        json=MUTATION_BODIES.get(path) if method == "POST" else None,
        params={"path": "acme_billing_summary.txt"} if path.endswith("/file") else None,
    )


def test_api_exposes_no_routes_beyond_the_reviewed_set(tmp_path: Path) -> None:
    assert set(api_routes(tmp_path)) == EXPECTED_ROUTES


def test_every_task_and_data_route_requires_a_session(app, tmp_path: Path) -> None:
    with new_client(app) as client:
        for path, methods in api_routes(tmp_path).items():
            if path in META_ROUTES:
                continue
            for method in methods:
                response = call(client, method, path)
                assert response.status_code == 401, (method, path, response.text)


def test_no_task_route_reaches_another_sessions_task(app, settings: Settings) -> None:
    with new_client(app) as alice, new_client(app) as bob:
        start_session(alice)
        start_session(bob)
        task_id = wait_for(alice, submit(alice, EMPLOYEE_GOAL)["task_id"], {"needs_approval"})[
            "task_id"
        ]
        for path, methods in api_routes(Path(settings.public_root).parent).items():
            if "{task_id}" not in path:
                continue
            for method in methods:
                response = call(bob, method, path, task_id)
                assert response.status_code == 404, (method, path, response.text)
        detail = alice.get(f"/api/v1/tasks/{task_id}").json()
        assert detail["status"] == "needs_approval" and detail["tool_calls"] == []
        assert employee_title(session_db(settings, alice)) == "Software Engineer"


def test_session_cookie_is_http_only_strict_and_secure(app) -> None:
    with new_client(app) as client:
        response = client.post("/api/v1/session", json={})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    body = response.json()
    assert body["authenticated"] is True
    assert body["approver"].startswith("demo-session:")
    token = response.cookies[SESSION_COOKIE]
    assert token not in response.text


def test_sessions_are_isolated(app, settings: Settings) -> None:
    with new_client(app) as alice, new_client(app) as bob:
        start_session(alice)
        start_session(bob)
        task = submit(alice, EMPLOYEE_GOAL)
        task_id = wait_for(alice, task["task_id"], {"needs_approval"})["task_id"]

        assert bob.get("/api/v1/tasks").json()["total"] == 0
        assert bob.get(f"/api/v1/tasks/{task_id}").status_code == 404
        assert bob.get(f"/api/v1/tasks/{task_id}/history").status_code == 404
        stolen = bob.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        assert stolen.status_code == 404
        assert alice.get(f"/api/v1/tasks/{task_id}").json()["status"] == "needs_approval"

        approved = alice.post(
            f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True}
        )
        assert approved.json()["status"] == "completed"
        assert employee_title(session_db(settings, alice)) == "Senior Engineer"
        assert employee_title(session_db(settings, bob)) == "Software Engineer"
        bob_e17 = next(
            e for e in bob.get("/api/v1/company/employees").json()["employees"]
            if e["employee_id"] == "E-17"
        )
        assert bob_e17["title"] == "Software Engineer"

        note = submit(alice, TICKET_NOTE_GOAL)
        wait_for(alice, note["task_id"], {"completed"})
        assert [f["path"] for f in alice.get("/api/v1/workspace/files").json()["files"]] == [
            "acme_billing_summary.txt"
        ]
        assert bob.get("/api/v1/workspace/files").json()["files"] == []
        assert bob.get("/api/v1/company/tickets").json()["tickets"] == []


def test_approval_records_the_session_identity(app) -> None:
    with new_client(app) as client:
        session = start_session(client)
        task_id = wait_for(client, submit(client, EMPLOYEE_GOAL)["task_id"], {"needs_approval"})[
            "task_id"
        ]
        client.post(f"/api/v1/tasks/{task_id}/approve", json={"step_id": "step_1", "confirm": True})
        events = client.get(f"/api/v1/tasks/{task_id}/history").json()["events"]
    approved = next(e for e in events if e["event_type"] == "approval.approved")
    assert approved["result"]["approver"] == session["approver"]


def test_cross_session_paths_cannot_be_read(app) -> None:
    with new_client(app) as alice, new_client(app) as bob:
        start_session(alice)
        start_session(bob)
        wait_for(alice, submit(alice, TICKET_NOTE_GOAL)["task_id"], {"completed"})
        alice_id = alice.app.state.aae.sessions.resolve(alice.cookies.get(SESSION_COOKIE)).session_id
        for path in (
            f"../../{alice_id}/workspace/acme_billing_summary.txt",
            "../data/runtime.db",
            "acme_billing_summary.txt",
        ):
            response = bob.get("/api/v1/workspace/file", params={"path": path})
            assert response.status_code in (400, 404), path


def test_access_code_gates_session_creation(tmp_path: Path) -> None:
    settings = public_settings(tmp_path, public_access_code="demo-code-2026")
    with new_client(create_app(settings)) as client:
        assert client.get("/api/v1/session").json()["access_code_required"] is True
        wrong = client.post("/api/v1/session", json={"access_code": "nope"})
        assert wrong.status_code == 403
        assert client.post("/api/v1/session", json={}).status_code == 403
        right = client.post("/api/v1/session", json={"access_code": "demo-code-2026"})
        assert right.status_code == 201
        assert "demo-code-2026" not in right.text
        assert "demo-code-2026" not in client.get("/").text


def test_task_quota_per_session(tmp_path: Path) -> None:
    settings = public_settings(tmp_path, public_max_tasks_per_session=2)
    with new_client(create_app(settings)) as client:
        start_session(client)
        for _ in range(2):
            wait_for(client, submit(client, TICKET_GOAL)["task_id"], {"completed"})
        over = client.post("/api/v1/tasks", json={"goal": TICKET_GOAL})
        assert over.status_code == 429
        assert over.json()["error"]["code"] == "task_quota_exceeded"
        assert client.get("/api/v1/tasks").json()["total"] == 2
        assert len(client.get("/api/v1/company/tickets").json()["tickets"]) == 2
        assert client.get("/api/v1/session").json()["tasks_used"] == 2


def test_session_capacity(tmp_path: Path) -> None:
    settings = public_settings(tmp_path, public_max_sessions=1)
    with new_client(create_app(settings)) as first, new_client(first.app) as second:
        start_session(first)
        response = second.post("/api/v1/session", json={})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "demo_capacity"


def test_mutation_rate_limit(tmp_path: Path) -> None:
    settings = public_settings(tmp_path, mutation_rate_limit_per_minute=2)
    with new_client(create_app(settings)) as client:
        start_session(client)
        codes = [
            client.post("/api/v1/tasks", json={"goal": "unsupported"}).status_code
            for _ in range(3)
        ]
        assert codes[:2] == [422, 422]
        assert codes[2] == 429
        limited = client.post("/api/v1/tasks", json={"goal": TICKET_GOAL})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1
        assert client.get("/api/v1/tasks").json()["total"] == 0


def test_session_creation_rate_limit(tmp_path: Path) -> None:
    settings = public_settings(tmp_path, session_rate_limit_per_minute=2)
    with new_client(create_app(settings)) as client:
        codes = [client.post("/api/v1/session", json={}).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


def test_forwarded_client_address_is_used_only_when_trusted(tmp_path: Path) -> None:
    settings = public_settings(
        tmp_path, session_rate_limit_per_minute=1, trust_proxy_headers=True
    )
    with new_client(create_app(settings)) as client:
        first = client.post(
            "/api/v1/session", json={}, headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}
        )
        spoofed = client.post(
            "/api/v1/session", json={}, headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}
        )
        other = client.post("/api/v1/session", json={}, headers={"X-Forwarded-For": "10.0.0.2"})
    assert [first.status_code, spoofed.status_code, other.status_code] == [201, 429, 201]


def test_expired_sessions_are_refused_and_erased(app) -> None:
    with new_client(app) as client:
        start_session(client)
        sessions = client.app.state.aae.sessions
        session = sessions.resolve(client.cookies.get(SESSION_COOKIE))
        wait_for(client, submit(client, TICKET_GOAL)["task_id"], {"completed"})
        assert session.root.exists()

        now = sessions._clock()
        sessions._clock = lambda: now + sessions.ttl_seconds + 1
        response = client.get("/api/v1/tasks")
        assert response.status_code == 401
        sessions.purge_expired(lambda _sid: False)
        assert not session.root.exists()


def test_restart_invalidates_sessions_instead_of_reporting_stale_success(
    settings: Settings,
) -> None:
    with new_client(create_app(settings)) as client:
        start_session(client)
        task_id = submit(client, TICKET_GOAL)["task_id"]
        wait_for(client, task_id, {"completed"})
        boot = client.get("/api/v1/health").json()["boot_id"]
        cookie = client.cookies.get(SESSION_COOKIE)

    with new_client(create_app(settings)) as restarted:
        assert restarted.get("/api/v1/health").json()["boot_id"] != boot
        restarted.cookies.set(SESSION_COOKIE, cookie)
        response = restarted.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 401
        assert "restart" in response.json()["error"]["message"]
    assert list(Path(settings.public_root).iterdir()) == []


def test_public_mode_hardening(app) -> None:
    with new_client(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        health = client.get("/api/v1/health")
        assert "max-age" in health.headers["strict-transport-security"]
        assert client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 200
        assert client.get("/api/v1/scenarios", headers={"Host": "evil.example"}).status_code == 400


def test_public_mode_requires_an_explicit_host_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        create_app(public_settings(tmp_path, allowed_hosts=""))


def test_render_hostname_is_accepted(tmp_path: Path) -> None:
    settings = public_settings(
        tmp_path, allowed_hosts="", render_external_hostname="aae-demo.onrender.com"
    )
    with TestClient(create_app(settings), base_url="https://aae-demo.onrender.com") as client:
        assert client.get("/api/v1/scenarios").status_code == 200
