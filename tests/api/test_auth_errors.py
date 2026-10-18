from types import SimpleNamespace

import pytest

from api_app.app import create_app
from video_agent.errors import VideoAgentError


def test_missing_and_incorrect_tokens_have_uniform_401(api):
    for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Basic abc"}):
        response = api.client.get("/api/v1/tasks", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_business_token_cannot_admin(api, auth, admin):
    assert api.client.get("/api/v1/admin/settings", headers=auth).status_code == 403
    assert api.client.get("/api/v1/tasks", headers=admin).status_code == 200


@pytest.mark.parametrize("business,admin", [("", "a"), ("b", ""), ("same", "same")])
def test_equal_or_missing_tokens_prevent_startup(business, admin):
    with pytest.raises(ValueError):
        create_app(SimpleNamespace(business_token=business, admin_token=admin))


def test_invalid_input_has_uniform_422(api, auth):
    response = api.client.patch("/api/v1/tasks/missing/inputs", headers=auth, json={"unexpected": "secret-input"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "secret-input" not in response.text
    assert api.client.get("/api/v1/tasks?limit=101", headers=auth).status_code == 422
    assert api.client.post("/api/v1/tasks", headers={**auth, "Idempotency-Key": "bad\tkey"}, json={}).status_code == 422


def test_errors_and_failure_views_never_expose_provider_secrets(api, auth, monkeypatch):
    secret = "sk-private-test-secret"
    def broken(*args, **kwargs):
        raise VideoAgentError("UPSTREAM_FAILED", f"https://user:{secret}@private.invalid C:\\secret-path", details={"api_key": secret})
    monkeypatch.setattr(api.agent, "get_task", broken)
    response = api.client.get("/api/v1/tasks/example", headers=auth)
    assert response.status_code == 503
    assert secret not in response.text
    assert "secret-path" not in response.text
    assert "Traceback" not in response.text


def test_health_live_and_ready_without_worker(api):
    assert api.client.get("/health/live").status_code == 200
    response = api.client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_stored_failure_stage_detail_is_redacted(api, auth, monkeypatch):
    from video_agent.models import FailureView, TaskView
    secret = "sk-stored-provider-secret"
    failure = FailureView(code="PROVIDER_TEMPORARY", message=secret, retryable=True)
    view = TaskView(task_id="example", status="failed", stage="planning", stage_detail="https://user:" + secret + "@private.invalid/path", failure=failure, updated_at="2026-10-14T00:00:00+00:00")
    monkeypatch.setattr(api.agent, "get_task", lambda task_id: view)
    response = api.client.get("/api/v1/tasks/example", headers=auth)
    assert response.status_code == 200
    assert secret not in response.text


def test_admin_settings_only_accept_public_settings(api, admin):
    revision = api.settings.read_settings().configuration_revision
    response = api.client.patch("/api/v1/admin/settings", headers=admin, json={"expected_version": revision, "planner": {"adapter": "fake"}})
    assert response.status_code == 422


def test_database_outage_is_sanitized_503(api, auth, monkeypatch):
    from sqlalchemy.exc import OperationalError
    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT test", {}, Exception("password=private-database-secret C:/private/path"))
    monkeypatch.setattr(api.agent, "list_tasks_page", unavailable)
    response = api.client.get("/api/v1/tasks", headers=auth)
    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True
    assert "private" not in response.text


def test_nested_task_and_plan_fields_are_strict(api, auth):
    for body in ({"product_info": {"unrecognized": "value"}}, {"video_options": {"unrecognized": True}}, {"audio_options": {"unrecognized": True}}):
        assert api.client.post("/api/v1/tasks", headers=auth, json=body).status_code == 422
    assert api.client.post("/api/v1/tasks/missing/plan-revisions", headers=auth, json={"patch": {"unrecognized": True}}).status_code == 422
    assert api.client.post("/api/v1/tasks/missing/plan-revisions", headers=auth, json={"patch": {"shot_texts": [{"shot_id": "s1", "unrecognized": True}]}}).status_code == 422


def test_artifact_quality_failure_does_not_expose_provider_message(api, auth, monkeypatch):
    from video_agent.models import ArtifactView
    artifact = ArtifactView(artifact_id="a", task_id="t", generation_id="g", plan_version=1, version=1, duration_seconds=5, aspect_ratio="9:16", width=180, height=320, quality_status="not_assessed", quality={"status": "not_assessed", "reason": "sk-quality-secret C:/private/video.mp4", "findings": []}, created_at="2026-10-14T00:00:00+00:00")
    monkeypatch.setattr(api.agent, "list_artifacts", lambda task_id: [artifact])
    response = api.client.get("/api/v1/tasks/t/artifacts", headers=auth)
    assert response.status_code == 200
    assert "sk-quality-secret" not in response.text
    assert "C:/private" not in response.text


def test_readiness_requires_fresh_active_worker_lease(api, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from api_app.routes import health
    from video_agent.worker_main import acquire_worker, release_worker
    # This unit test isolates lease age; real executable detection/encoding is
    # exercised separately by the end-to-end contract.
    monkeypatch.setattr(health.shutil, "which", lambda executable: "available")
    owner = "readiness-worker"
    assert acquire_worker(api.agent.db, owner, api.agent.clock)
    assert api.client.get("/health/ready").status_code == 200
    now = datetime.now(timezone.utc)
    with api.agent.db.transaction() as connection:
        connection.execute("UPDATE worker_heartbeats SET last_seen_at=:seen, lease_until=:until WHERE singleton_id=1", {"seen": (now - timedelta(seconds=31)).isoformat(), "until": (now + timedelta(seconds=30)).isoformat()})
    response = api.client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["details"]["checks"]["worker"] is False
    with api.agent.db.transaction() as connection:
        connection.execute("UPDATE worker_heartbeats SET last_seen_at=:seen, stopping=1 WHERE singleton_id=1", {"seen": now.isoformat()})
    assert api.client.get("/health/ready").status_code == 503
    release_worker(api.agent.db, owner)
