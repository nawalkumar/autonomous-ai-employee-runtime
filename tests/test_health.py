"""Phase 0 smoke tests for application import and health endpoint."""

from fastapi.testclient import TestClient

from app.main import app, create_app


def test_app_imports_and_factory() -> None:
    assert app is not None
    assert create_app().title == "Autonomous AI Employee Runtime"


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
