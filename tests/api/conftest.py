import io
import os
import re
from types import SimpleNamespace

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from api_app.app import create_app
from video_agent.db import Database
from video_agent.service import VideoAgent
from video_agent.settings_service import SettingsService
from video_agent.runtime import RuntimeSettings


def pytest_addoption(parser):
    parser.addoption("--api-mysql", action="store_true", default=False, help="Run HTTP contracts against explicitly configured isolated MySQL 5.7+ test database")


@pytest.fixture(scope="session")
def api_mysql_database(request):
    if not request.config.getoption("--api-mysql"):
        yield None
        return
    from alembic import command
    from alembic.config import Config
    required = ("HOST", "PORT", "NAME", "USER", "PASSWORD")
    if any("VIDEO_AGENT_TEST_DB_" + field not in os.environ for field in required):
        pytest.fail("--api-mysql requires independent VIDEO_AGENT_TEST_DB_* variables", pytrace=False)
    name = os.environ["VIDEO_AGENT_TEST_DB_NAME"]
    if not re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", name) or name == os.environ.get("VIDEO_AGENT_DB_NAME", "video_agent"):
        pytest.fail("Refusing HTTP test mutation of a production or ambiguously named database", pytrace=False)
    settings = RuntimeSettings(db_host=os.environ["VIDEO_AGENT_TEST_DB_HOST"], db_port=int(os.environ["VIDEO_AGENT_TEST_DB_PORT"]), db_name=name, db_user=os.environ["VIDEO_AGENT_TEST_DB_USER"], db_password=os.environ["VIDEO_AGENT_TEST_DB_PASSWORD"])
    database = Database.from_settings(settings)
    configuration = Config("alembic.ini")
    with database.engine.connect() as connection:
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
    try:
        yield (database, settings)
    finally:
        database.close()


@pytest.fixture
def api(tmp_path, api_mysql_database):
    settings = RuntimeSettings(home=tmp_path, business_token="test-business-token", admin_token="test-admin-token", secret_key=Fernet.generate_key().decode())
    if api_mysql_database is None:
        database = Database(tmp_path)
        from video_agent.provider_jobs import ensure_worker_schema
        ensure_worker_schema(database)
        service = SettingsService(database, settings.secret_key)
        agent = VideoAgent(tmp_path, database=database, settings_service=service)
        app = create_app(settings, agent=agent, settings_service=service)
    else:
        from dataclasses import replace
        from video_agent.config_store import seed_default_settings
        database, database_settings = api_mysql_database
        settings = replace(database_settings, home=tmp_path, business_token=settings.business_token, admin_token=settings.admin_token, secret_key=settings.secret_key)
        # Only the guarded, explicitly named test database is cleared. Keep the
        # Alembic version and any unrelated tables untouched.
        tables = ("worker_heartbeats", "provider_jobs", "idempotency", "work_items", "artifacts", "generation_shots", "generations", "plan_versions", "tasks", "assets", "adapter_bindings", "model_profile_revisions", "model_profiles", "app_settings")
        with database.transaction() as connection:
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
            seed_default_settings(connection)
        app = create_app(settings)
        agent = app.state.agent
        service = app.state.settings_service
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, app=app, agent=agent, settings=service, runtime=settings)
    if api_mysql_database is None:
        database.close()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-business-token"}


@pytest.fixture
def admin():
    return {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
def png():
    image = io.BytesIO()
    Image.new("RGB", (32, 32), (150, 35, 45)).save(image, format="PNG")
    return image.getvalue()


def upload(api, auth, png):
    response = api.client.post("/api/v1/assets", headers=auth, data={"kind": "image"}, files={"file": ("photo.png", png, "image/png")})
    assert response.status_code == 201, response.text
    return response.json()["asset_id"]


def create(api, auth, png, **extra):
    asset = upload(api, auth, png)
    body = {"images": [asset], "product_info": {"name": "示例杯", "selling_points": ["轻便"]}, "user_prompt": "展示商品", **extra}
    response = api.client.post("/api/v1/tasks", headers=auth, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def plan(api, task_id):
    for _ in range(10):
        task = api.agent.get_task(task_id)
        if task.head_plan_version is not None:
            return task
        assert api.agent.tick().did_work
    raise AssertionError("规划未完成")
