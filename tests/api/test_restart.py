from fastapi.testclient import TestClient

from api_app.app import create_app
from video_agent.db import Database
from video_agent.service import VideoAgent
from video_agent.settings_service import SettingsService

from .conftest import create


def test_http_restart_preserves_queued_task_and_snapshot(api, auth, png):
    task = create(api, auth, png)
    with api.agent.db.transaction(write=False) as connection:
        before = connection.execute("SELECT copied_config_json FROM work_items WHERE task_id=:id", {"id": task["task_id"]}).fetchone()["copied_config_json"]
    if api.app.state.production:
        # Real production bootstrap reconnects to the same isolated MySQL.
        restarted = create_app(api.runtime)
        database = None
    else:
        database = Database(api.runtime.home)
        settings = SettingsService(database, api.runtime.secret_key)
        agent = VideoAgent(api.runtime.home, database=database, settings_service=settings)
        restarted = create_app(api.runtime, agent=agent, settings_service=settings)
    try:
        with TestClient(restarted) as client:
            response = client.get(f"/api/v1/tasks/{task['task_id']}", headers=auth)
            assert response.status_code == 200 and response.json()["status"] == "pending"
            with restarted.state.agent.db.transaction(write=False) as connection:
                after = connection.execute("SELECT copied_config_json FROM work_items WHERE task_id=:id", {"id": task["task_id"]}).fetchone()["copied_config_json"]
            assert after == before
            for _ in range(10):
                response = client.get(f"/api/v1/tasks/{task['task_id']}", headers=auth)
                if response.json()["head_plan_version"] is not None:
                    break
                assert restarted.state.agent.tick().did_work
            assert response.json()["head_plan_version"] == 1
    finally:
        if database is not None:
            database.close()
